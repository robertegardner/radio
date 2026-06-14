#!/usr/bin/env python3
"""Scan FM band, log stations, optionally grab RDS PS names.

Uses SoapySDR Python API with the SDRplay RSPdx-R2, Antenna A (Shakespeare 5120).
Opens the device once, retunes per channel — no per-channel device open/close.

Writes /var/lib/sdr-streams/stations.json consumed by the Flask UI.
"""
import argparse, json, select, statistics, subprocess, sys, time
from datetime import datetime
from pathlib import Path

import numpy as np
import SoapySDR

FM_LOW    = 87.9e6
FM_HIGH   = 108.1e6
FIRST_CHAN = 88.1e6
LAST_CHAN  = 107.9e6
CHAN_STEP  = 0.2e6
SAMP_RATE  = 250_000   # 250 kHz → ~200 kHz IF bandwidth; limits window to ≈1 FM channel width


SRC_ENV = Path("/etc/radio-compute/source-dx-r2.env")


def device_args() -> str:
    """SoapySDR device args. On the rack (radio-compute) the dx-R2 is REMOTE —
    read SOAPY_ARGS (driver=remote,...,remote:driver=sdrplay) from the source env.
    On the Pi (file absent) fall back to the local driver=sdrplay. One script,
    both tiers — the rack has no local SDR so a hardcoded driver=sdrplay fails."""
    if SRC_ENV.exists():
        for line in SRC_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("SOAPY_ARGS="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "driver=sdrplay"


def channels():
    f = FIRST_CHAN
    while f <= LAST_CHAN + 1e3:
        yield f
        f += CHAN_STEP


def measure_band(gain: float, settle_ms: int, dwell_ms: int,
                 antenna: str = "Antenna A"):
    """Open device once, sweep all FM channels, return {freq_hz: power_db} dict."""
    settle_s = settle_ms / 1000.0
    dwell_s  = dwell_ms  / 1000.0

    dev_args = device_args()
    sdr = SoapySDR.Device(SoapySDR.KwargsFromString(dev_args))
    sdr.setAntenna(SoapySDR.SOAPY_SDR_RX, 0, antenna)
    sdr.setGainMode(SoapySDR.SOAPY_SDR_RX, 0, False)   # disable AGC
    sdr.setGain(SoapySDR.SOAPY_SDR_RX, 0, float(gain))
    sdr.setSampleRate(SoapySDR.SOAPY_SDR_RX, 0, SAMP_RATE)

    # Remote dx-R2: force the IQ onto lossless TCP (SoapyRemote's UDP firehose
    # drops datagrams). Harmless/ignored on a local open.
    stream_args = {"remote:prot": "tcp"} if "remote" in dev_args else {}
    rxStream = sdr.setupStream(SoapySDR.SOAPY_SDR_RX, SoapySDR.SOAPY_SDR_CF32, [0], stream_args)
    sdr.activateStream(rxStream)

    buf_len   = 4096
    buf       = np.zeros(buf_len, dtype=np.complex64)
    settle_n  = max(1, int(SAMP_RATE * settle_s / buf_len))
    dwell_n   = max(1, int(SAMP_RATE * dwell_s  / buf_len))

    measurements = {}
    chan_list = list(channels())
    t0 = time.time()

    try:
        for i, freq in enumerate(chan_list):
            if i % 25 == 0:
                print(f"[scan] {i}/{len(chan_list)} channels, "
                      f"{int(time.time()-t0)}s elapsed", file=sys.stderr)
            sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, freq)

            # settle
            for _ in range(settle_n):
                sdr.readStream(rxStream, [buf], buf_len,
                               timeoutUs=int(settle_s * 2e6))

            # dwell — accumulate power
            power_acc = 0.0
            n_reads = 0
            for _ in range(dwell_n):
                r = sdr.readStream(rxStream, [buf], buf_len,
                                   timeoutUs=int(dwell_s * 2e6))
                if r.ret > 0:
                    power_acc += float(np.mean(np.abs(buf[:r.ret])**2))
                    n_reads += 1

            if n_reads > 0:
                power_linear = power_acc / n_reads
                db = 10.0 * np.log10(max(power_linear, 1e-12))
                measurements[freq] = db
    finally:
        sdr.deactivateStream(rxStream)
        sdr.closeStream(rxStream)

    return measurements


def grab_rds(freq_mhz: float, gain: float, seconds: int = 12):
    """Run rx_fm+redsea briefly and return the first PS name seen (or None).

    Uses select() to honor the timeout even when redsea produces no output."""
    try:
        rtl = subprocess.Popen(
            ["rx_fm", "-d", "driver=sdrplay", "-a", "Antenna A",
             "-M", "fm", "-l", "0", "-A", "std",
             "-s", "250000", "-g", str(gain), "-f", f"{freq_mhz}M", "-F", "9", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        redsea = subprocess.Popen(
            ["redsea", "-r", "250000", "--output", "json"],
            stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        rtl.stdout.close()
    except FileNotFoundError as e:
        print(f"[scan] rds probe skipped: {e}", file=sys.stderr)
        return None

    ps = None
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([redsea.stdout], [], [],
                                        deadline - time.time())
            if not ready:
                break
            line = redsea.stdout.readline()
            if not line:
                break
            try:
                import json as _json
                d = _json.loads(line)
                ps = d.get("ps", "").strip()
                if ps:
                    break
            except Exception:
                pass
    finally:
        try: rtl.kill()
        except OSError: pass
        try: redsea.kill()
        except OSError: pass
        try: rtl.wait(timeout=3)
        except Exception: pass
        try: redsea.wait(timeout=3)
        except Exception: pass

    return ps or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gain",       default=40,   type=float)
    ap.add_argument("--threshold-db", default=10, type=float)
    ap.add_argument("--settle-ms",  default=150,  type=int)
    ap.add_argument("--dwell-ms",   default=300,  type=int)
    ap.add_argument("--rds",        action="store_true",
                    help="probe each detected station for RDS PS name (slow)")
    ap.add_argument("--out", default="/var/lib/sdr-streams/stations.json")
    args = ap.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    chan_list = list(channels())
    print(f"[scan] sweeping {len(chan_list)} FM channels @ gain {args.gain}, "
          f"{args.settle_ms}ms settle + {args.dwell_ms}ms dwell, 2 MSps",
          file=sys.stderr)
    t0 = time.time()

    measurements = measure_band(args.gain, args.settle_ms, args.dwell_ms)
    elapsed = int(time.time() - t0)
    print(f"[scan] done in {elapsed}s, {len(measurements)} channels measured",
          file=sys.stderr)

    if not measurements:
        print("[scan] no measurements collected", file=sys.stderr)
        sys.exit(1)

    db_values = list(measurements.values())
    noise = statistics.median(db_values)
    print(f"[scan] noise floor ~{noise:.1f} dB", file=sys.stderr)

    stations = []
    for freq, db in measurements.items():
        snr = db - noise
        if snr >= args.threshold_db:
            freq_mhz = round(freq / 1e6, 1)
            entry = {
                "freq_mhz": freq_mhz,
                "snr_db":   round(snr, 1),
                "power_db": round(db, 1),
            }
            if args.rds:
                print(f"[scan] RDS probe {freq_mhz} MHz …", file=sys.stderr)
                ps = grab_rds(freq_mhz, args.gain)
                if ps:
                    entry["ps"] = ps
                    print(f"[scan]   → {ps}", file=sys.stderr)
            stations.append(entry)

    stations.sort(key=lambda s: -s["snr_db"])
    out = {
        "scanned_at":     datetime.now().isoformat(timespec="seconds"),
        "noise_floor_db": round(noise, 1),
        "stations":       stations,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"[scan] wrote {len(stations)} stations to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
