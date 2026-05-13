#!/usr/bin/env python3
"""Scan FM band, log stations, optionally grab RDS PS names.

Writes /var/lib/sdr-streams/stations.json consumed by the Flask UI.
"""
import argparse, csv, json, select, statistics, subprocess, sys, time
from pathlib import Path
from datetime import datetime

FM_LOW, FM_HIGH = 87.9, 108.1
CHAN_SPACING = 0.2
FIRST_CHAN = 88.1
LAST_CHAN  = 107.9


def channels():
    f = FIRST_CHAN
    while f <= LAST_CHAN + 1e-6:
        yield round(f, 1)
        f += CHAN_SPACING


def run_rtl_power(out, gain, integ, dur, bin_khz):
    cmd = ["rtl_power", "-f", f"{FM_LOW}M:{FM_HIGH}M:{bin_khz}k",
           "-i", str(integ), "-e", str(dur), "-g", str(gain), str(out)]
    print(f"[scan] {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, check=True)


def parse_rtl_power(path):
    bins = {}
    with open(path) as f:
        for row in csv.reader(f):
            if len(row) < 7:
                continue
            try:
                hz_low = float(row[2]); hz_step = float(row[4])
                powers = [float(x) for x in row[6:] if x.strip()]
            except ValueError:
                continue
            for i, p in enumerate(powers):
                center = int(hz_low + (i + 0.5) * hz_step)
                bins.setdefault(center, []).append(p)
    return bins


def power_at(bins, target_hz, tol=75_000):
    samples = [p for k, vs in bins.items() if abs(k - target_hz) <= tol for p in vs]
    return statistics.mean(samples) if samples else None


def grab_rds(freq_mhz, gain, seconds=12):
    """Tune briefly and try to read RDS PS via redsea. Returns PS string or None.

    Uses select() to honor the timeout even when redsea produces no output."""
    try:
        rtl = subprocess.Popen(
            ["rtl_fm", "-M", "fm", "-l", "0", "-A", "std",
             "-s", "171000", "-g", str(gain), "-f", f"{freq_mhz}M", "-F", "9", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        redsea = subprocess.Popen(
            ["redsea", "-r", "171000", "--output", "json"],
            stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        rtl.stdout.close()
    except FileNotFoundError as e:
        print(f"[rds] {e}", file=sys.stderr)
        return None

    ps = None
    deadline = time.time() + seconds
    fd = redsea.stdout.fileno()

    try:
        while time.time() < deadline:
            remaining = deadline - time.time()
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if not ready:
                continue
            line = redsea.stdout.readline()
            if not line:
                break
            try:
                rec = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if rec.get("ps"):
                ps = rec["ps"].strip()
                break
    finally:
        for p in (redsea, rtl):
            try: p.kill()
            except Exception: pass
            try: p.wait(timeout=2)
            except Exception: pass
    return ps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gain",     default=40, type=float)
    ap.add_argument("--duration", default=60, type=int)
    ap.add_argument("--integ",    default=5,  type=int)
    ap.add_argument("--bin-khz",  default=25, type=int)
    ap.add_argument("--threshold-db", default=8, type=float)
    ap.add_argument("--rds", action="store_true",
                    help="Tune each detected station briefly to grab RDS PS")
    ap.add_argument("--out",
                    default="/var/lib/sdr-streams/stations.json")
    args = ap.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    raw = Path("/tmp/fm_power.csv")

    run_rtl_power(raw, args.gain, args.integ, args.duration, args.bin_khz)
    bins = parse_rtl_power(raw)

    all_means = [statistics.mean(v) for v in bins.values() if v]
    if not all_means:
        print("[scan] no data", file=sys.stderr)
        sys.exit(1)
    noise = statistics.median(all_means)
    print(f"[scan] noise floor ~{noise:.1f} dB", file=sys.stderr)

    stations = []
    for f_mhz in channels():
        f_hz = int(f_mhz * 1_000_000)
        p = power_at(bins, f_hz)
        if p is None:
            continue
        snr = p - noise
        if snr < args.threshold_db:
            continue
        st = {
            "freq_mhz": f_mhz,
            "snr_db":   round(snr, 1),
            "power_db": round(p, 1),
            "ps":       None,
        }
        if args.rds:
            print(f"[rds] {f_mhz} MHz...", file=sys.stderr)
            st["ps"] = grab_rds(f_mhz, args.gain)
            if st["ps"]:
                print(f"[rds]    -> {st['ps']}", file=sys.stderr)
        stations.append(st)

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
