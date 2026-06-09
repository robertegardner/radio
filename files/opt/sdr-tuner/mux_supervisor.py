#!/usr/bin/env python3
"""mux_supervisor.py — launch/reap one channel pipeline per active FM channel.

FM-multistation Phase 2. Reads /etc/sdr-streams/channels.json and runs one
channel_pipeline.sh per channel, each subscribing to the shared IQ stream
(iq_capture.py) and sourcing its own Icecast mount. This is the "more of the
same" layer on top of the Phase-1 demod stage: an extra station is just another
supervised pipeline.

The supervisor never touches the SDR — iq_capture owns it. sdr-mux.service
Requires=sdr-iq-capture.service, and the whole FM-multistation mode is mutually
exclusive with sdr-fm@active / AM / wxsat (one device). Flask writes
channels.json and signals us; it does not manage processes.

Mount derivation (deterministic, not user-chosen, to avoid collisions):
  primary channel -> fm.mp3  (so the existing UI/captions keep working)
  others          -> m{freq}.mp3  with '.' -> '_'  (e.g. 95.7 -> m95_7.mp3)

Signals:
  SIGHUP   reload channels.json — stop removed/changed pipelines, start new ones,
           leave unchanged pipelines running (don't drop unaffected channels).
  SIGTERM  stop every pipeline and exit.

Status: /run/sdr-streams/mux_status.json (channel set + per-channel liveness,
plus the capture's ADC health read from iq_capture.json).
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CHANNELS_PATH = Path("/etc/sdr-streams/channels.json")
STATUS_PATH = Path("/run/sdr-streams/mux_status.json")
IQ_STATUS_PATH = Path("/run/sdr-streams/iq_capture.json")
PIPELINE = "/opt/sdr-tuner/channel_pipeline.sh"
MUX_ENV = "/etc/sdr-streams/mux.env"

# Clean ceiling on this Pi 5 sharing cores with the scanner's SDRTrunk: 3
# channels run with zero IQ drops; the 4th glitches (~43 drops/s) — the remaining
# cost is stereo_decode (numpy) + the per-channel python IQ fanout. Lifting this
# needs the deferred polyphase channelizer / a C stereo decoder (out of scope).
MAX_CHANNELS = 3
RESTART_BACKOFF_S = 5.0  # min gap between respawns of a crashing pipeline


def read_env(path: str = MUX_ENV) -> dict:
    env: dict[str, str] = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def mount_for(ch: dict) -> str:
    if ch.get("primary"):
        return "fm.mp3"
    return "m" + str(ch["freq"]).replace(".", "_") + ".mp3"


def load_channels(window: tuple[float, float]) -> dict[str, dict]:
    """Return desired channels keyed by mount. Validated + normalized.

    Enforces: <=4 channels, freqs inside the advertised window, exactly one
    primary (first wins; if none, the first channel is promoted).
    """
    try:
        raw = json.loads(CHANNELS_PATH.read_text())
        chans = raw.get("channels", []) if isinstance(raw, dict) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

    lo, hi = window
    out: dict[str, dict] = {}
    seen_primary = False
    for ch in chans[:MAX_CHANNELS]:
        try:
            freq = float(ch["freq"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (lo <= freq <= hi):
            sys.stderr.write(f"mux: skipping {freq} — outside window [{lo}, {hi}]\n")
            continue
        primary = bool(ch.get("primary")) and not seen_primary
        if primary:
            seen_primary = True
        norm = {
            "freq": freq,
            "stereo": bool(ch.get("stereo", True)),
            "rds": bool(ch.get("rds", False)),
            "primary": primary,
            "bitrate": str(ch.get("bitrate", "192k")),
        }
        norm["mount"] = mount_for(norm)
        out[norm["mount"]] = norm
    # Promote a primary if none was flagged (so /fm.mp3 + now_playing.json exist).
    if out and not seen_primary:
        first = next(iter(out.values()))
        del out[first["mount"]]
        first["primary"] = True
        first["mount"] = mount_for(first)
        out[first["mount"]] = first
    return out


def spec_key(ch: dict) -> tuple:
    """Identity for change detection — restart a pipeline only if these differ."""
    return (ch["freq"], ch["stereo"], ch["rds"], ch["primary"], ch["bitrate"])


class Pipeline:
    def __init__(self, ch: dict):
        self.ch = ch
        self.key = spec_key(ch)
        self.proc: subprocess.Popen | None = None
        self.last_start = 0.0

    def start(self):
        ch = self.ch
        args = [PIPELINE, str(ch["freq"]), ch["mount"],
                "1" if ch["stereo"] else "0", ch["bitrate"],
                "1" if ch["rds"] else "0", "1" if ch["primary"] else "0"]
        # start_new_session so we can signal the whole pipeline process group.
        self.proc = subprocess.Popen(args, start_new_session=True)
        self.last_start = time.monotonic()
        sys.stderr.write(f"mux: started {ch['mount']} ({ch['freq']} MHz "
                         f"stereo={ch['stereo']} rds={ch['rds']} "
                         f"primary={ch['primary']}) pid={self.proc.pid}\n")
        sys.stderr.flush()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        sys.stderr.write(f"mux: stopped {self.ch['mount']}\n")
        sys.stderr.flush()


def write_status(pipelines: dict[str, Pipeline], state: str = "running") -> None:
    iq = {}
    try:
        iq = json.loads(IQ_STATUS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    doc = {
        "role": "mux",
        "state": state,
        "pid": os.getpid(),
        "updated": int(time.time()),
        "capture": {
            "adc_peak_pct": iq.get("adc_peak_pct"),
            "adc_rms": iq.get("adc_rms"),
            "clients": iq.get("clients"),
            "drops": iq.get("drops"),
            "overflows": iq.get("overflows"),
        },
        "channels": [
            {"freq": p.ch["freq"], "mount": p.ch["mount"],
             "stereo": p.ch["stereo"], "rds": p.ch["rds"],
             "primary": p.ch["primary"], "bitrate": p.ch["bitrate"],
             "alive": p.alive()}
            for p in pipelines.values()
        ],
    }
    try:
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        tmp.replace(STATUS_PATH)
    except OSError:
        pass


def main() -> int:
    env = read_env()
    window = (float(env.get("WINDOW_LO_MHZ", "95.0")),
              float(env.get("WINDOW_HI_MHZ", "101.0")))

    pipelines: dict[str, Pipeline] = {}
    reload_flag = {"v": True}   # start by loading channels.json
    running = {"v": True}

    def on_hup(*_):
        reload_flag["v"] = True

    def on_term(*_):
        running["v"] = False

    signal.signal(signal.SIGHUP, on_hup)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    def reconcile():
        desired = load_channels(window)
        # Stop pipelines that are gone or whose spec changed.
        for mount in list(pipelines):
            if mount not in desired or spec_key(desired[mount]) != pipelines[mount].key:
                pipelines[mount].stop()
                del pipelines[mount]
        # Start pipelines that are new (or were just removed for a spec change).
        for mount, ch in desired.items():
            if mount not in pipelines:
                p = Pipeline(ch)
                p.start()
                pipelines[mount] = p
        sys.stderr.write(f"mux: reconciled — {len(pipelines)} channel(s): "
                         f"{', '.join(pipelines)}\n")
        sys.stderr.flush()

    last_status = 0.0
    while running["v"]:
        if reload_flag["v"]:
            reload_flag["v"] = False
            reconcile()
        # Reap/restart crashed pipelines that are still desired.
        for mount, p in list(pipelines.items()):
            if not p.alive() and (time.monotonic() - p.last_start) >= RESTART_BACKOFF_S:
                rc = p.proc.poll() if p.proc else None
                sys.stderr.write(f"mux: {mount} exited (rc={rc}) — restarting\n")
                sys.stderr.flush()
                p.start()
        now = time.monotonic()
        if now - last_status >= 2.0:
            last_status = now
            write_status(pipelines)
        time.sleep(0.5)

    sys.stderr.write("mux: shutting down — stopping all channels\n")
    for p in pipelines.values():
        p.stop()
    write_status({}, state="stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
