#!/usr/bin/env python3
"""Go / no-go signal check for a retained wxsat baseband.cs16.

A failed LRPT decode (Viterbi NOSYNC, BER ~0.42, SNR 0 dB) looks the same
whether the satellite was never heard or the demod just couldn't lock. This
tool answers the prior question — *was there any signal at the antenna?* — by
walking the whole recorded IQ and measuring in-band power (where LRPT lives,
DC +/- HALF_KHZ) against a clean adjacent reference band, window by window
across the pass.

A real pass shows a flat-topped QPSK hump several dB above the reference that
rises and falls with elevation. No in-band excess (and no pass-shaped bump in
the level series) means the signal never reached the receiver — an antenna /
feedline / orientation problem, NOT a decode bug. Use it to decide whether the
next lever is RF hardware or demod tuning.

Reads files only — never touches the SDR, safe anytime (even mid-capture,
though it only sees the IQ written so far).

CLI:
    wxsat_cn_check.py <capture-id> ...   # specific captures by id
    wxsat_cn_check.py <dir> ...          # specific capture dirs
    wxsat_cn_check.py --all              # every retained pass (has baseband.cs16)
"""
import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

import wxsat_predict as predict

WXSAT_DIR = predict.WXSAT_DIR
CAPTURES_PATH = WXSAT_DIR / "captures.json"

# Verdict thresholds on peak in-band-minus-reference excess (dB). LRPT QPSK is
# wideband and noise-like, so even a strong pass is only ~6-12 dB over the
# adjacent band here; what matters is that the excess is clearly positive and
# tracks the pass. Below ~1.5 dB there is effectively nothing in-band.
SIGNAL_DB = 3.0
MARGINAL_DB = 1.5


def _load_captures():
    try:
        d = json.loads(CAPTURES_PATH.read_text())
        return d.get("captures", []) if isinstance(d, dict) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _fs_from_log(cap_dir):
    """Recover the capture sample rate from capture.log (authoritative)."""
    try:
        text = (cap_dir / "capture.log").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"Sampling at (\d+) S/s", text)
    return int(m.group(1)) if m else None


def cn_check(cap_dir, rec, nfft=32768, navg=4, inband_khz=70.0,
             ref_lo_khz=200.0, ref_hi_khz=350.0):
    """Walk the whole IQ; return (verdict, stats, profile) or (None, msg, None)."""
    iq = cap_dir / "baseband.cs16"
    if not iq.exists() or iq.stat().st_size < nfft * navg * 4:
        return None, "no usable baseband.cs16", None

    fs = _fs_from_log(cap_dir) or int(
        float(predict.load_config().get("samplerate", 1000000)))

    raw = np.memmap(iq, dtype="<i2", mode="r")
    n_cplx = raw.shape[0] // 2
    win = np.hanning(nfft).astype(np.float32)
    fr = np.fft.fftfreq(nfft, 1.0 / fs)
    m_in = np.abs(fr) < inband_khz * 1e3
    m_ref = (np.abs(fr) >= ref_lo_khz * 1e3) & (np.abs(fr) < ref_hi_khz * 1e3)

    aos = int(rec.get("aos_unix") or 0)
    los = int(rec.get("los_unix") or 0)
    span = (los - aos) if los > aos else 0

    # ~2 s per window, averaging navg FFTs within it for a stable PSD.
    win_cplx = max(nfft * navg, fs * 2)
    n_win = max(1, n_cplx // win_cplx)
    profile = []  # (t_unix, rms, inband_db, ref_db, cn_db)
    for wi in range(n_win):
        base = wi * win_cplx
        acc = np.zeros(nfft)
        sumsq = cnt = 0.0
        for k in range(navg):
            b = base + k * nfft
            if b + nfft > n_cplx:
                break
            seg = np.asarray(raw[2 * b:2 * (b + nfft)], dtype=np.float32)
            iqc = seg[0::2] + 1j * seg[1::2]
            acc += np.abs(np.fft.fft(iqc * win)) ** 2
            sumsq += float(np.mean(seg * seg)); cnt += 1
        if cnt == 0:
            continue
        psd = acc / cnt
        inb = 10.0 * np.log10(float(np.mean(psd[m_in])) + 1e-9)
        ref = 10.0 * np.log10(float(np.mean(psd[m_ref])) + 1e-9)
        rms = float(np.sqrt(sumsq / cnt))
        t = aos + (wi / max(1, n_win - 1)) * span if span else wi * 2
        profile.append((int(t), rms, round(inb, 2), round(ref, 2),
                        round(inb - ref, 2)))

    if not profile:
        return None, "IQ too short to profile", None

    cn = np.array([p[4] for p in profile])
    rmsv = np.array([p[1] for p in profile])
    peak_cn = float(cn.max())
    verdict = ("signal" if peak_cn >= SIGNAL_DB
               else "marginal" if peak_cn >= MARGINAL_DB else "none")
    stats = {
        "fs": fs,
        "duration_s": int(n_cplx / fs),
        "windows": len(profile),
        "peak_cn_db": round(peak_cn, 2),
        "median_cn_db": round(float(np.median(cn)), 2),
        "adc_fill_pct": round(100.0 * float(rmsv.max()) / 32767.0, 2),
        "iq_gb": round(iq.stat().st_size / 1e9, 2),
        "inband_khz": inband_khz,
        "ref_khz": [ref_lo_khz, ref_hi_khz],
    }
    return verdict, stats, profile


def _resolve(target, caps):
    """Map an id or dir argument to (cap_dir, rec) — mirrors wxsat_rebuild."""
    rec = next((c for c in caps if c.get("id") == target), None)
    if rec and rec.get("outdir"):
        return WXSAT_DIR / str(rec["outdir"]), rec
    p = Path(target)
    if p.is_dir():
        rec = next((c for c in caps if c.get("outdir") == p.name), {})
        return p, rec
    return None, None


_MARK = {"signal": "ok", "marginal": "??", "none": "--"}


def _report(cap_dir, rec, verdict, stats, profile, show_profile):
    elev = rec.get("max_elev")
    elev_s = f"{elev:.0f}°" if isinstance(elev, (int, float)) else "?"
    print(f"{_MARK.get(verdict, '!!')} {cap_dir.name}  (max elev {elev_s})")
    if profile is None:
        print(f"     {stats}")  # stats holds the error message here
        return
    print(f"     verdict: {verdict.upper()}  "
          f"peak in-band excess {stats['peak_cn_db']:+.2f} dB "
          f"(median {stats['median_cn_db']:+.2f})")
    print(f"     fs={stats['fs']} dur={stats['duration_s']}s "
          f"ADC fill peak {stats['adc_fill_pct']:.1f}%  IQ {stats['iq_gb']}G  "
          f"in-band ±{stats['inband_khz']:.0f}k vs ref "
          f"{stats['ref_khz'][0]:.0f}-{stats['ref_khz'][1]:.0f}k")
    if verdict == "none":
        print("     -> no in-band signal across the pass: RF path "
              "(antenna / feedline / orientation), not a decode fault")
    if show_profile:
        step = max(1, len(profile) // 24)
        print("      t(s)   rms   inband   ref    excess(dB)")
        t0 = profile[0][0]
        for p in profile[::step]:
            print(f"     {p[0]-t0:5d}  {p[1]:5.0f}  {p[2]:7.1f} {p[3]:6.1f}   {p[4]:+5.2f}")


def main():
    ap = argparse.ArgumentParser(
        description="Go/no-go signal check on a retained wxsat baseband.cs16")
    ap.add_argument("targets", nargs="*", help="capture ids or dirs")
    ap.add_argument("--all", action="store_true",
                    help="check every retained pass (has baseband.cs16)")
    ap.add_argument("--profile", action="store_true",
                    help="print the per-window power profile across the pass")
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
            print(f"?? {t}: not found", file=sys.stderr)
            continue
        jobs.append((d, rec or {}))

    if not jobs:
        print("nothing to check (no retained IQ found)")
        return
    for cap_dir, rec in jobs:
        try:
            verdict, stats, profile = cn_check(cap_dir, rec)
        except Exception as e:  # never let one bad pass abort the batch
            print(f"!! {cap_dir.name}: {e}", file=sys.stderr)
            continue
        _report(cap_dir, rec, verdict, stats, profile, args.profile)


if __name__ == "__main__":
    main()
