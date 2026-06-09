#!/usr/bin/env python3
"""Rebuild a reviewable pass panel from a retained baseband.cs16.

The live sidecar (wxsat_live.py) builds its waterfall from the *tail* of the
growing IQ in real time, so its snapshot is only meaningful for a live capture.
For a pass whose raw IQ was retained (debug mode / a failed decode), this tool
reconstructs a faithful, time-varying `pass.json` by walking the WHOLE file:

  - a waterfall of `rows` spectra spread across the recording,
  - peak/rms signal level over the pass,
  - the sky track (az/el arc) from the cached TLE,
  - a decode summary scraped from capture.log (best SNR, final sync, synced?).

It writes the same `<capture-dir>/pass.json` the gallery's "Pass view" reads,
tagged `source:"rebuilt"`. Reads files only — never touches the SDR, safe anytime.

CLI:
    wxsat_rebuild.py --all              # every retained pass missing/any panel
    wxsat_rebuild.py <capture-id> ...   # specific captures by id
    wxsat_rebuild.py <dir> ...          # specific capture dirs
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

import wxsat_predict as predict
from wxsat_live import SkyTrack

WXSAT_DIR = predict.WXSAT_DIR
CAPTURES_PATH = WXSAT_DIR / "captures.json"


def _load_captures():
    try:
        d = json.loads(CAPTURES_PATH.read_text())
        return d.get("captures", []) if isinstance(d, dict) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _scan_log(log_path):
    """Decode summary from the full capture.log (not just a tail)."""
    out = {"decode_pct": None, "snr": None, "viterbi": None, "deframer": None,
           "synced": None, "best_snr": None}
    fs = freq_hz = None
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return out, fs, freq_hz
    snrs = [float(m) for m in re.findall(r"SNR :\s*([-\d.]+)dB", text)]
    if snrs:
        out["best_snr"] = round(max(snrs), 1)
        out["snr"] = round(snrs[-1], 1)
    pcts = re.findall(r"Progress\s+([\d.]+)%", text)
    if pcts:
        out["decode_pct"] = round(float(pcts[-1]), 1)
    if "Viterbi : SYNC" in text:
        out["viterbi"] = "SYNC"
    elif "Viterbi : NOSYNC" in text:
        out["viterbi"] = "NOSYNC"
    if "Deframer : SYNC" in text:
        out["deframer"] = "SYNC"
    elif "Deframer : NOSYNC" in text:
        out["deframer"] = "NOSYNC"
    if "bytes of CADUs" in text:
        out["synced"] = True
    elif "no pipeline synced" in text:
        out["synced"] = False
    m = re.search(r"Sampling at (\d+) S/s", text)
    if m:
        fs = int(m.group(1))
    m = re.search(r"Tuned to (\d+) Hz", text)
    if m:
        freq_hz = int(m.group(1))
    return out, fs, freq_hz


def rebuild(cap_dir, rec, rows=240, nfft=4096, bins=256, navg=4):
    iq = cap_dir / "baseband.cs16"
    if not iq.exists() or iq.stat().st_size < nfft * 4:
        return None, "no retained baseband.cs16"

    decode, fs_log, freq_hz = _scan_log(cap_dir / "capture.log")
    fs = fs_log or int(float(predict.load_config().get("samplerate", 1000000)))
    freq_mhz = (freq_hz / 1e6) if freq_hz else 137.9

    raw = np.memmap(iq, dtype="<i2", mode="r")
    n_cplx = raw.shape[0] // 2
    win = np.hanning(nfft).astype(np.float32)
    starts = np.linspace(0, max(0, n_cplx - nfft * navg), rows).astype(np.int64)

    aos = int(rec.get("aos_unix") or 0)
    los = int(rec.get("los_unix") or 0)
    span = (los - aos) if los > aos else 0

    waterfall, level = [], []
    for ri, s0 in enumerate(starts):
        acc = np.zeros(nfft)
        peak, sumsq, cnt = 0.0, 0.0, 0
        for k in range(navg):
            b = int(s0) + k * nfft
            if b + nfft > n_cplx:
                break
            seg = np.asarray(raw[2 * b:2 * (b + nfft)], dtype=np.float32)
            iqc = seg[0::2] + 1j * seg[1::2]
            acc += np.abs(np.fft.fftshift(np.fft.fft(iqc * win))) ** 2
            peak = max(peak, float(np.abs(seg).max()))
            sumsq += float(np.mean(seg * seg)); cnt += 1
        if cnt == 0:
            continue
        psd = 10.0 * np.log10(acc / cnt + 1e-9)
        psd -= np.median(psd)
        psd = np.clip(psd, -5.0, 45.0)
        psd = psd.reshape(bins, nfft // bins).mean(axis=1)
        waterfall.append([int(round(v)) for v in psd])
        t = aos + (ri / max(1, rows - 1)) * span if span else int(time.time())
        level.append([int(t), round(100.0 * peak / 32767.0, 2),
                      round(float(np.sqrt(sumsq / cnt)), 1)])

    track = SkyTrack(str(rec.get("norad") or ""),
                     *(lambda c: (c["lat"], c["lon"], c["alt_km"]))(predict.load_config()))
    arc = track.arc(aos, los) if span else []

    snap = {
        "satellite": rec.get("satellite"), "norad": rec.get("norad"),
        "aos_unix": aos, "los_unix": los,
        "max_elev": rec.get("max_elev") if rec.get("max_elev") is not None
                    else (round(max((r[1] for r in arc)), 1) if arc else None),
        "samplerate": fs, "freq_mhz": round(freq_mhz, 3),
        "half_khz": round(min(250.0, fs / 2000.0), 1),
        "waterfall": waterfall, "level": level, "track": arc,
        "decode": decode, "saved": int(time.time()), "source": "rebuilt",
    }
    out = cap_dir / "pass.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap))
    os.replace(tmp, out)
    return out, f"{len(waterfall)} spectra, fs={fs}, {iq.stat().st_size/1e9:.1f}G IQ"


def _resolve(target, caps):
    """Map an id or dir argument to (cap_dir, rec)."""
    rec = next((c for c in caps if c.get("id") == target), None)
    if rec and rec.get("outdir"):
        return WXSAT_DIR / str(rec["outdir"]), rec
    p = Path(target)
    if p.is_dir():
        rec = next((c for c in caps if c.get("outdir") == p.name), {})
        return p, rec
    return None, None


def main():
    ap = argparse.ArgumentParser(description="Rebuild wxsat pass panels from retained IQ")
    ap.add_argument("targets", nargs="*", help="capture ids or dirs")
    ap.add_argument("--all", action="store_true",
                    help="rebuild every retained pass (has baseband.cs16)")
    args = ap.parse_args()
    caps = _load_captures()

    jobs = []
    if args.all:
        for c in caps:
            if c.get("outdir") and (WXSAT_DIR / str(c["outdir"]) / "baseband.cs16").exists():
                jobs.append((WXSAT_DIR / str(c["outdir"]), c))
    for t in args.targets:
        d, rec = _resolve(t, caps)
        if d is None:
            print(f"  ?? {t}: not found", file=sys.stderr)
            continue
        jobs.append((d, rec or {}))

    if not jobs:
        print("nothing to rebuild (no retained IQ found)")
        return
    for cap_dir, rec in jobs:
        try:
            out, msg = rebuild(cap_dir, rec)
        except Exception as e:  # never let one bad pass abort the batch
            print(f"  !! {cap_dir.name}: {e}", file=sys.stderr)
            continue
        if out:
            print(f"  ok {cap_dir.name}: {msg}")
        else:
            print(f"  -- {cap_dir.name}: {msg}")


if __name__ == "__main__":
    main()
