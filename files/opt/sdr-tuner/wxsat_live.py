#!/usr/bin/env python3
"""Live per-pass telemetry for the /wxsat page.

A short-lived sidecar that `wxsat_capture.sh` launches in the background for the
lifetime of one capture. It produces /run/sdr-streams/wxsat_live.json a few times
a second so the web UI can show, in real time, *what the tuner is hearing and
seeing*:

  recording phase  — a spectrum/waterfall row + signal level read straight from
                     the growing baseband.cs16, plus the satellite's live az/el
                     and the pass arc (from the cached TLE via pyorbital).
  decoding phase   — SatDump's progress %, SNR and Viterbi/Deframer sync state,
                     scraped from the per-pass capture.log.

This is BEST-EFFORT and must never affect the capture: every loop is wrapped so
an error just skips a frame. It reads the IQ read-only (rx_sdr owns the writer)
and never touches the SDR. The capture script kills it on exit; it also
self-terminates when the decode finishes or a hard deadline passes.

Inputs (env, set by wxsat_capture.sh):
  WXSAT_OUT_DIR   the per-pass dir (holds baseband.cs16 + capture.log)
  WXSAT_AOS WXSAT_LOS WXSAT_SAT WXSAT_NORAD   pass metadata (for the sky track)
  SAMPLERATE FREQ_MHZ                          tuning (for axis labels)
"""
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

LIVE_PATH = Path("/run/sdr-streams/wxsat_live.json")
TLE_DIR = Path("/var/lib/sdr-streams/wxsat/tle")

FFT_RAW = 4096          # FFT size taken from the IQ tail
FFT_BINS = 256          # bins sent on the wire (raw averaged down to this)
TAIL_SAMPLES = 131072   # complex samples read from the end of the file per frame
POLL_S = 1.5            # update cadence


def _atomic_write(payload):
    try:
        LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = LIVE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, LIVE_PATH)
    except OSError:
        pass


def _spectrum_and_level(iq_path):
    """Return (fft_db_list, rms, peak_pct) from the tail of the growing IQ file,
    or (None, None, None) if not enough data yet."""
    try:
        size = iq_path.stat().st_size
    except OSError:
        return None, None, None
    nbytes = TAIL_SAMPLES * 4  # cs16 = 2 x int16 per complex sample
    start = max(0, (size - nbytes) // 4 * 4)
    try:
        with open(iq_path, "rb") as f:
            f.seek(start)
            buf = f.read(nbytes)
    except OSError:
        return None, None, None
    a = np.frombuffer(buf, dtype="<i2")
    a = a[: (a.size // 2) * 2].astype(np.float32)
    if a.size < 2 * FFT_RAW:
        return None, None, None
    iq = a[0::2] + 1j * a[1::2]
    rms = float(np.sqrt(np.mean(a * a)))
    peak_pct = round(100.0 * float(np.abs(a).max()) / 32767.0, 2)

    # Average a few FFT windows from the tail for a stable row.
    win = np.hanning(FFT_RAW).astype(np.float32)
    acc = np.zeros(FFT_RAW)
    nwin = min(8, iq.size // FFT_RAW)
    for k in range(nwin):
        seg = iq[k * FFT_RAW:(k + 1) * FFT_RAW]
        acc += np.abs(np.fft.fftshift(np.fft.fft(seg * win))) ** 2
    psd = 10.0 * np.log10(acc / nwin + 1e-9)
    psd -= np.median(psd)                       # noise floor ~ 0 dB
    psd = psd.reshape(FFT_BINS, FFT_RAW // FFT_BINS).mean(axis=1)
    psd = np.clip(psd, -5.0, 45.0)
    return [round(float(v), 1) for v in psd], round(rms, 1), peak_pct


class SkyTrack:
    """Satellite az/el via pyorbital + the cached TLE. Degrades to None silently
    (no TLE / no pyorbital → the UI just omits the sky plot)."""

    def __init__(self, norad, lat, lon, alt_km):
        self.ok = False
        self.lat, self.lon, self.alt = lat, lon, alt_km
        try:
            from pyorbital.orbital import Orbital
            lines = (TLE_DIR / f"{norad}.tle").read_text().splitlines()
            self.orb = Orbital(lines[0].strip(), line1=lines[1].strip(),
                               line2=lines[2].strip())
            self.ok = True
        except Exception:
            self.orb = None

    def look(self, unix_t):
        if not self.ok:
            return None
        try:
            dt = datetime.fromtimestamp(unix_t, timezone.utc).replace(tzinfo=None)
            az, el = self.orb.get_observer_look(dt, self.lon, self.lat, self.alt)
            return round(float(az), 1), round(float(el), 1)
        except Exception:
            return None

    def arc(self, aos, los, n=48):
        if not (self.ok and los > aos):
            return []
        out = []
        for k in range(n + 1):
            t = aos + (los - aos) * k / n
            lk = self.look(t)
            if lk:
                out.append([int(t), lk[1], lk[0]])  # [unix, el, az]
        return out


def _parse_decode(log_path):
    """Latest progress %, SNR, and Viterbi/Deframer sync from capture.log.
    Returns a dict (possibly partial) and a 'done' flag."""
    out = {"decode_pct": None, "snr": None, "viterbi": None,
           "deframer": None, "done": False, "synced": None}
    try:
        # Only the tail matters; read the last chunk.
        size = log_path.stat().st_size
        with open(log_path, "r", errors="replace") as f:
            f.seek(max(0, size - 16384))
            lines = f.read().splitlines()
    except OSError:
        return out
    for ln in lines:
        if "SNR :" in ln and "Progress" in ln:
            try:
                out["snr"] = round(float(ln.split("SNR :")[1].split("dB")[0]), 1)
                out["decode_pct"] = round(float(ln.split("Progress")[1]
                                                 .split("%")[0]), 1)
            except (ValueError, IndexError):
                pass
        if "Viterbi :" in ln:
            try:
                out["viterbi"] = ln.split("Viterbi :")[1].split("BER")[0].strip()
            except IndexError:
                pass
        if "Deframer :" in ln:
            out["deframer"] = "SYNC" if "Deframer : SYNC" in ln else "NOSYNC"
        if "synced" in ln and "bytes of CADUs" in ln:
            out["synced"] = True
        if "no pipeline synced" in ln:
            out["synced"] = False
        if "capture complete" in ln or "no pipeline synced" in ln:
            out["done"] = True
    return out


def main():
    out_dir = Path(os.environ.get("WXSAT_OUT_DIR", ""))
    if not out_dir.name:
        return
    iq_path = out_dir / "baseband.cs16"
    log_path = out_dir / "capture.log"
    aos = int(os.environ.get("WXSAT_AOS") or 0)
    los = int(os.environ.get("WXSAT_LOS") or 0)
    sat = os.environ.get("WXSAT_SAT") or "Meteor-M"
    norad = os.environ.get("WXSAT_NORAD") or ""
    try:
        fs = int(float(os.environ.get("SAMPLERATE", "1000000")))
    except ValueError:
        fs = 1000000
    try:
        freq_mhz = float(os.environ.get("FREQ_MHZ", "137.9"))
    except ValueError:
        freq_mhz = 137.9

    # Observer location (same loader the predictor/scheduler use).
    lat, lon, alt = 37.31, -89.55, 0.1
    try:
        import wxsat_predict as predict
        cfg = predict.load_config()
        lat, lon, alt = cfg["lat"], cfg["lon"], cfg["alt_km"]
    except Exception:
        pass

    track = SkyTrack(norad, lat, lon, alt)
    arc = track.arc(aos, los) if (aos and los) else []

    # Persistent, reviewable snapshot — written into the capture dir so the panel
    # can be replayed from the gallery long after the pass (and after the IQ is
    # pruned). The live /run frame is ephemeral; this one survives.
    snapshot_path = out_dir / "pass.json"
    wf_rows, lvl = [], []          # accumulated waterfall rows + level samples
    best_snr = [None]              # boxed so the snapshot closure sees updates
    last_decode = [{}]
    half_khz = min(250.0, fs / 2000.0)

    def save_snapshot():
        def thin(seq, m=240):
            if len(seq) <= m:
                return seq
            step = len(seq) / m
            return [seq[int(i * step)] for i in range(m)]
        snap = {
            "satellite": sat, "norad": norad, "aos_unix": aos, "los_unix": los,
            "max_elev": (round(max((r[1] for r in arc)), 1) if arc else None),
            "samplerate": fs, "freq_mhz": freq_mhz, "half_khz": round(half_khz, 1),
            "waterfall": thin(wf_rows), "level": thin(lvl), "track": arc,
            "decode": {**last_decode[0], "best_snr": best_snr[0]},
            "saved": int(time.time()),
        }
        try:
            tmp = snapshot_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap))
            os.replace(tmp, snapshot_path)
        except OSError:
            pass

    def _on_term(*_):
        save_snapshot()
        os._exit(0)
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass

    deadline = time.time() + (max(1800, (los - time.time()) + 1200) if los else 1800)
    tick = 0
    while time.time() < deadline:
        try:
            now = time.time()
            tick += 1
            # Phase comes from capture.log markers, not IQ-growth: the capture
            # script logs "wxsat: decoding" when the SDR is done and the offline
            # decode begins, and a terminal line when finished. (IQ-growth is
            # unreliable — it's flat both before rx_sdr starts and after it ends.)
            logtext = _read_tail(log_path)
            decoding = "wxsat: decoding" in logtext
            done = ("wxsat: capture complete" in logtext
                    or "no pipeline synced" in logtext)

            payload = {
                "updated": int(now), "satellite": sat, "norad": norad,
                "aos_unix": aos, "los_unix": los,
                "samplerate": fs, "freq_mhz": freq_mhz,
                "phase": "decoding" if decoding else "recording",
            }

            if decoding:
                dec = _parse_decode(log_path)
                payload.update(dec)
                payload["done"] = done or dec.get("done")
                last_decode[0] = {k: dec.get(k) for k in
                                  ("decode_pct", "snr", "viterbi", "deframer", "synced")}
                if dec.get("snr") is not None:
                    best_snr[0] = dec["snr"] if best_snr[0] is None else max(best_snr[0], dec["snr"])
                _atomic_write(payload)
                if tick % 8 == 0:
                    save_snapshot()
                if payload["done"]:
                    break
            else:
                fft, rms, peak = _spectrum_and_level(iq_path)
                lk = track.look(now)
                if fft is not None:
                    wf_rows.append(fft)
                    lvl.append([int(now), peak, rms])
                payload.update({
                    "fft": fft, "rms": rms, "peak_pct": peak,
                    "elev": lk[1] if lk else None,
                    "azim": lk[0] if lk else None,
                    "track": arc,
                })
                _atomic_write(payload)
                if tick % 8 == 0:
                    save_snapshot()
        except Exception:
            pass
        time.sleep(POLL_S)

    save_snapshot()
    # Drop the ephemeral live frame; the page hides the live panel when the tuner
    # leaves capturing/decoding and when `updated` ages out.
    try:
        LIVE_PATH.unlink()
    except OSError:
        pass


def _read_tail(log_path, n=524288):
    """Last n bytes of the log as text (enough to hold the phase markers even
    after SatDump dumps its long pipeline list). Empty string if unreadable."""
    try:
        size = log_path.stat().st_size
        with open(log_path, "r", errors="replace") as f:
            f.seek(max(0, size - n))
            return f.read()
    except OSError:
        return ""


if __name__ == "__main__":
    main()
