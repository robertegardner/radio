#!/usr/bin/env python3
"""iq_capture.py — own the dx-R2 and fan raw wideband IQ out to channel pipelines.

This is the device-owning daemon for FM-multistation mode. It opens the SDRplay
RSPdx-R2 ONCE (Antenna A), captures a wide CF32 block (default 98.0 MHz center,
8 Msps → ±4 MHz), and publishes the raw IQ to one or more channel pipelines over
a Unix-domain socket. Each pipeline shifts/decimates/demods its own station out
of the shared stream (see channel_pipeline.sh), so an extra station is just
another subscriber — no second device open.

Two modes
---------
server  (default)  Open the device, publish CF32 blocks on a SOCK_SEQPACKET UNIX
                   socket. SEQPACKET makes every send atomic: a nonblocking send
                   either queues a WHOLE block or fails with EWOULDBLOCK, so a
                   slow consumer just loses entire (sample-aligned) blocks — the
                   byte stream can never desync mid-sample. The device read is
                   never blocked by a slow subscriber.
--subscribe        Connect to that socket and copy the IQ byte stream to stdout.
                   This is the bridge that feeds a csdr pipeline's stdin; kept in
                   this file (rather than a separate helper or `nc -U`) so the
                   fanout contract lives in one place.

Capture in CF32 directly: the SoapySDRPlay3 driver does the CS16→CF32 conversion
in C, so Python never touches per-sample data — it just forwards bytes. This is
what lets a 64 MB/s stream hold on the Pi.

Device contention: the dx-R2 can be opened by exactly one process. This daemon,
the legacy sdr-fm@active stream, AM tuning, and the wxsat SatDump capture are all
mutually exclusive (same rule sdr-fm@active already lives under). The systemd
unit declares Conflicts=sdr-fm@active.service; the rollback to known-good mono is
always `systemctl stop sdr-iq-capture && systemctl start sdr-fm@active`.

Config: /etc/sdr-streams/mux.env (CENTER_MHZ, FS, IF_BW, ANTENNA, GAIN, IQ_SOCKET).
"""
import argparse
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import numpy as np

MUX_ENV = "/etc/sdr-streams/mux.env"
# Capture-side status. The mux supervisor owns mux_status.json and folds this in;
# in Phase 1 (no mux) this file is the capture's standalone status.
STATUS_PATH = Path("/run/sdr-streams/iq_capture.json")

# 65536 complex samples → 512 KB CF32 packets. Comfortably under the 4 MB
# wmem_max/SO_SNDBUF ceiling (one SEQPACKET datagram must fit in the send
# buffer), and ~8.2 ms of audio per block at 8 Msps. SO_SNDBUF below buffers a
# handful of these so a brief consumer hiccup drops nothing.
BLOCK_COMPLEX = 65536
SNDBUF = 4 * 1024 * 1024
# Subscriber recv buffer must be >= the largest packet or SEQPACKET truncates it.
RECV_BUF = 1024 * 1024


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


# --------------------------------------------------------------------------
# subscribe mode — the bridge that feeds a csdr pipeline
# --------------------------------------------------------------------------
def subscribe(sock_path: str) -> int:
    """Connect to the capture socket and copy IQ bytes to stdout."""
    # Reconnect with backoff: the capture daemon may start slightly after us
    # (mux launches pipelines; systemd may order us before capture is ready).
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SNDBUF)
    deadline = time.monotonic() + 30.0
    while True:
        try:
            s.connect(sock_path)
            break
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            if time.monotonic() > deadline:
                sys.stderr.write(f"iq_capture --subscribe: capture socket {sock_path} "
                                 "not available after 30s\n")
                return 1
            time.sleep(0.25)

    out = sys.stdout.buffer
    try:
        while True:
            data = s.recv(RECV_BUF)
            if not data:  # server closed the connection
                return 0
            out.write(data)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0  # downstream csdr exited — normal teardown
    finally:
        try:
            s.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# server mode — own the device, fan out IQ
# --------------------------------------------------------------------------
def open_device(env: dict):
    """Open + configure the dx-R2 for wideband FM capture. Returns (sdr, rx)."""
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

    center = float(env.get("CENTER_MHZ", "98.0")) * 1e6
    fs = float(env.get("FS", "8000000"))
    if_bw = float(env.get("IF_BW", "8000000"))
    antenna = env.get("ANTENNA", "Antenna A")
    gain = float(env.get("GAIN", "40"))
    # Explicit dx-R2 gain elements (preferred over the overall-gain abstraction).
    # IFGR = IF gain reduction [20..59], RFGR = RF gain reduction / LNA state
    # [0..27] — higher = MORE attenuation for both. Capturing the whole 6 MHz
    # band overloads the front end with the overall knob (it maxes IFGR but
    # leaves RFGR~1, LNA wide open → ADC clips). RFGR is the lever for overload:
    # RFGR=9 was the first LNA step that cleared it (peak ~49%, rms ~25%) on the
    # Shakespeare antenna. Verified by /tmp/gain_probe.py 2026-06-09; watch the
    # startup ADC log / mux_status.json adc_peak_pct after any antenna change.
    gain_ifgr = env.get("GAIN_IFGR")
    gain_rfgr = env.get("GAIN_RFGR")

    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, fs)
    sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    try:
        sdr.setBandwidth(SOAPY_SDR_RX, 0, if_bw)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"iq_capture: setBandwidth({if_bw}) failed: {e}\n")
    # Manual gain. The aggregate power of the WHOLE 6 MHz window hits the ADC at
    # once (more overload risk than single-station), so we set a fixed gain and
    # watch the ADC meter rather than letting hardware AGC pump. NOTE: GAIN here
    # is SoapySDR's overall-gain abstraction; verify direction + headroom against
    # the startup ADC-fill log (the dx-R2's element gains can be counter-intuitive
    # — see CLAUDE.md / project memory on the inverted rx_sdr -g).
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"iq_capture: setGainMode(False) failed: {e}\n")
    if gain_ifgr is not None or gain_rfgr is not None:
        # Element control: RFGR first (front-end / overload), then IFGR (level).
        if gain_rfgr is not None:
            sdr.setGain(SOAPY_SDR_RX, 0, "RFGR", float(gain_rfgr))
        if gain_ifgr is not None:
            sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", float(gain_ifgr))
    else:
        sdr.setGain(SOAPY_SDR_RX, 0, gain)
    sdr.setFrequency(SOAPY_SDR_RX, 0, center)

    # State dump (mirrors am_stream.py): the driver may quantize/clamp requests
    # and silently no-op unknown keys, so log what actually took effect.
    sys.stderr.write("iq_capture: ---- SDR state ----\n")
    sys.stderr.write(
        f"iq_capture: antenna={sdr.getAntenna(SOAPY_SDR_RX, 0)!r} "
        f"fs={sdr.getSampleRate(SOAPY_SDR_RX, 0):.0f} "
        f"freq={sdr.getFrequency(SOAPY_SDR_RX, 0):.0f} "
        f"bw={sdr.getBandwidth(SOAPY_SDR_RX, 0):.0f} "
        f"total_gain={sdr.getGain(SOAPY_SDR_RX, 0):.2f}\n"
    )
    for elem in sdr.listGains(SOAPY_SDR_RX, 0):
        try:
            sys.stderr.write(f"iq_capture:   gain[{elem}]={sdr.getGain(SOAPY_SDR_RX, 0, elem):.2f} dB\n")
        except Exception:  # noqa: BLE001
            pass
    sys.stderr.write("iq_capture: -------------------\n")
    sys.stderr.flush()

    rx = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(rx)
    return sdr, rx


def make_listener(sock_path: str) -> socket.socket:
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o660)  # radio-group pipelines connect
    srv.listen(8)
    srv.setblocking(False)
    return srv


def write_status(state: str, **extra) -> None:
    """Atomic status write for the /wxsat-style UI + journald-free introspection."""
    doc = {"role": "iq-capture", "state": state, "updated": int(time.time())}
    doc.update(extra)
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        tmp.replace(STATUS_PATH)
    except OSError:
        pass


def server(env: dict) -> int:
    import SoapySDR  # noqa: F401  (imported for the constant below via open_device)
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_OVERFLOW

    sock_path = env.get("IQ_SOCKET", "/run/sdr-streams/iq.sock")
    fs = float(env.get("FS", "8000000"))

    sdr, rx = open_device(env)
    srv = make_listener(sock_path)
    sys.stderr.write(f"iq_capture: serving CF32 @ {fs/1e6:.1f} Msps on {sock_path}\n")
    sys.stderr.flush()

    clients: list[socket.socket] = []
    drops: dict[int, int] = {}

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    buf = np.empty(BLOCK_COMPLEX * 2, dtype=np.float32)  # interleaved I/Q
    overflows = 0
    blocks = 0
    peak = 0.0
    rms = 0.0
    last_status = 0.0

    while running:
        # Accept any pending subscribers (nonblocking; loop drains the backlog).
        while True:
            try:
                c, _ = srv.accept()
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            c.setblocking(False)
            c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
            clients.append(c)
            drops[id(c)] = 0
            sys.stderr.write(f"iq_capture: subscriber connected ({len(clients)} total)\n")
            sys.stderr.flush()

        sr = sdr.readStream(rx, [buf], BLOCK_COMPLEX, timeoutUs=1_000_000)
        if sr.ret == SOAPY_SDR_OVERFLOW or sr.ret == -4:
            overflows += 1
            continue
        if sr.ret <= 0:
            continue
        n = sr.ret
        # memoryview avoids copying the float array; .tobytes() once, send to all.
        data = buf[: 2 * n].tobytes()
        blocks += 1

        dead = []
        for c in clients:
            try:
                c.send(data)
            except (BlockingIOError, InterruptedError):
                drops[id(c)] += 1  # consumer too slow — drop this whole block
            except (BrokenPipeError, ConnectionResetError, OSError):
                dead.append(c)
        for c in dead:
            clients.remove(c)
            drops.pop(id(c), None)
            sys.stderr.write(f"iq_capture: subscriber dropped ({len(clients)} total)\n")
            sys.stderr.flush()

        # ADC overload meter every ~16 blocks (~1 s). CF32 from Soapy is
        # normalized to +/-1.0 full scale; peak near 1.0 == clipping (invariant 5).
        if blocks % 16 == 0:
            seg = buf[: 2 * n]
            peak = float(np.abs(seg).max())
            rms = float(np.sqrt(np.mean(seg * seg)))
        now = time.monotonic()
        if now - last_status >= 2.0:
            last_status = now
            total_drops = sum(drops.values())
            write_status("capturing", clients=len(clients), overflows=overflows,
                         drops=total_drops, adc_peak=round(peak, 4),
                         adc_rms=round(rms, 4),
                         adc_peak_pct=round(100.0 * peak, 1))
            if peak >= 0.98:
                sys.stderr.write(f"iq_capture: WARNING ADC near clip peak={peak:.3f} "
                                 "— reduce GAIN (invariant 5)\n")
                sys.stderr.flush()

    write_status("stopped")
    try:
        sdr.deactivateStream(rx)
        sdr.closeStream(rx)
    except Exception:  # noqa: BLE001
        pass
    try:
        srv.close()
        os.unlink(sock_path)
    except OSError:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="dx-R2 wideband IQ capture + fanout")
    ap.add_argument("--subscribe", action="store_true",
                    help="bridge mode: copy the IQ socket to stdout (feeds csdr)")
    ap.add_argument("--socket", default=None, help="override IQ_SOCKET path")
    args = ap.parse_args()
    env = read_env()
    sock_path = args.socket or env.get("IQ_SOCKET", "/run/sdr-streams/iq.sock")
    if args.subscribe:
        return subscribe(sock_path)
    return server(env)


if __name__ == "__main__":
    sys.exit(main())
