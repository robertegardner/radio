#!/usr/bin/env python3
"""Walk the US AM broadcast band measuring per-channel signal strength.

Uses rtl_fm in direct-sampling mode (-E direct2) one channel at a time.
rtl_power's -D flag in apt's build is a boolean that doesn't accept the
Q-branch input (input 2) most cheap RTL-SDR dongles need for AM.
"""
import argparse
import array
import json
import math
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

FIRST_CHAN_HZ    = 540_000
LAST_CHAN_HZ     = 1_700_000
CHAN_SPACING_HZ  = 10_000
SAMPLE_RATE      = 12_000


def channels():
    f = FIRST_CHAN_HZ
    while f <= LAST_CHAN_HZ:
        yield f
        f += CHAN_SPACING_HZ


def measure_channel(freq_hz: int, gain: float,
                    settle_ms: int = 200, dwell_ms: int = 350):
    cmd = [
        "rtl_fm",
        "-M", "am",
        "-E", "direct2",
        "-f", str(freq_hz),
        "-s", str(SAMPLE_RATE),
        "-g", str(gain),
        "-l", "0",
        "-",
    ]
    settle_bytes = SAMPLE_RATE * 2 * settle_ms // 1000
    sample_bytes = SAMPLE_RATE * 2 * dwell_ms // 1000

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    except FileNotFoundError as e:
        print(f"[scan] rtl_fm not found: {e}", file=sys.stderr)
        return None

    data = b""
    try:
        deadline = time.time() + 3.0
        skip = settle_bytes
        while skip > 0 and time.time() < deadline:
            chunk = proc.stdout.read(min(skip, 4096))
            if not chunk:
                break
            skip -= len(chunk)
        deadline = time.time() + 3.0
        need = sample_bytes
        chunks = []
        while need > 0 and time.time() < deadline:
            chunk = proc.stdout.read(min(need, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            need -= len(chunk)
        data = b"".join(chunks)
    finally:
        try: proc.kill()
        except Exception: pass
        try: proc.wait(timeout=2)
        except Exception: pass

    if len(data) < sample_bytes // 2:
        return None

    arr = array.array('h')
    arr.frombytes(data[:len(data) // 2 * 2])
    if not arr:
        return None

    n = len(arr)
    mean = sum(arr) / n
    sumsq = sum((s - mean) * (s - mean) for s in arr)
    rms = math.sqrt(sumsq / n)
    if rms < 1.0:
        return -120.0
    return 20.0 * math.log10(rms / 32768.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gain", default=40, type=float)
    ap.add_argument("--threshold-db", default=10, type=float)
    ap.add_argument("--settle-ms", default=200, type=int)
    ap.add_argument("--dwell-ms",  default=350, type=int)
    ap.add_argument("--out", default="/var/lib/sdr-streams/stations_am.json")
    args = ap.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    chan_list = list(channels())
    print(f"[scan] walking {len(chan_list)} AM channels @ gain {args.gain}, "
          f"{args.settle_ms}ms settle + {args.dwell_ms}ms dwell each",
          file=sys.stderr)
    t0 = time.time()

    measurements = []
    for i, f_hz in enumerate(chan_list):
        if i % 25 == 0:
            print(f"[scan] {i}/{len(chan_list)} channels, "
                  f"{int(time.time() - t0)}s elapsed",
                  file=sys.stderr)
        db = measure_channel(f_hz, args.gain,
                             settle_ms=args.settle_ms, dwell_ms=args.dwell_ms)
        if db is not None:
            measurements.append((f_hz, db))

    elapsed = int(time.time() - t0)
    print(f"[scan] done in {elapsed}s, {len(measurements)} channels measured",
          file=sys.stderr)

    if not measurements:
        print("[scan] no measurements collected", file=sys.stderr)
        sys.exit(1)

    db_values = [db for _, db in measurements]
    noise = statistics.median(db_values)
    print(f"[scan] noise floor ~{noise:.1f} dBFS", file=sys.stderr)

    stations = []
    for f_hz, db in measurements:
        snr = db - noise
        if snr >= args.threshold_db:
            stations.append({
                "freq_khz": f_hz // 1000,
                "snr_db":   round(snr, 1),
                "power_db": round(db, 1),
            })

    stations.sort(key=lambda s: -s["snr_db"])
    out = {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "noise_floor_db": round(noise, 1),
        "stations": stations,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"[scan] wrote {len(stations)} stations to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
