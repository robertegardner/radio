#!/usr/bin/env python3
"""Walk the US AM broadcast band measuring per-channel signal strength.

Uses SoapySDR Python API with the SDRplay RSPdx-R2, Antenna C (long-wire AM).
No direct-sampling mode needed; the dx-R2 covers AM natively down to 1 kHz.

Strategy: sweep the AM band in a small number of hops using FFT-based power
measurement. Each hop captures SAMP_RATE Hz of bandwidth; the FFT resolves
individual 10 kHz AM channels within that window without re-opening the device.
"""
import argparse, json, statistics, sys, time
from datetime import datetime
from pathlib import Path

import numpy as np
import SoapySDR

FIRST_CHAN_HZ   = 540_000
LAST_CHAN_HZ    = 1_700_000
CHAN_SPACING_HZ = 10_000
SAMP_RATE       = 1_000_000   # 1 MSps → 2 MSps hardware (2× oversampling, dx-R2 supported)
FFT_SIZE        = 4096         # frequency resolution = SAMP_RATE / FFT_SIZE ≈ 244 Hz/bin


SRC_ENV = Path("/etc/radio-compute/source-dx-r2.env")


def device_args() -> str:
    """SoapySDR device args: REMOTE (driver=remote) from the rack source env if
    present, else local driver=sdrplay (the Pi). The rack has no local SDR."""
    if SRC_ENV.exists():
        for line in SRC_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("SOAPY_ARGS="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "driver=sdrplay"


def channels():
    f = FIRST_CHAN_HZ
    while f <= LAST_CHAN_HZ:
        yield f
        f += CHAN_SPACING_HZ


def fft_power_db(samples: np.ndarray) -> np.ndarray:
    """Return dB power spectrum (dBFS) for a block of CF32 samples."""
    windowed = samples * np.hanning(len(samples))
    spectrum = np.fft.fftshift(np.fft.fft(windowed, n=FFT_SIZE))
    power = (np.abs(spectrum) ** 2) / FFT_SIZE
    return 10.0 * np.log10(np.maximum(power, 1e-30))


def measure_band(gain: float, settle_ms: int, dwell_ms: int,
                 antenna: str = "Antenna C"):
    """
    Sweep the AM band.  For each tuning hop, capture DWELL seconds of IQ,
    compute FFT, then average the bin nearest each 10 kHz AM channel center.
    Returns {freq_hz: power_db}.
    """
    settle_s   = settle_ms / 1000.0
    dwell_s    = dwell_ms  / 1000.0
    half_bw    = SAMP_RATE / 2          # useful bandwidth around center
    hop_step   = int(half_bw * 1.8)    # ~90% overlap, more hops but less edge artefact
    bin_hz     = SAMP_RATE / FFT_SIZE   # Hz per FFT bin

    dev_args = device_args()
    sdr = SoapySDR.Device(SoapySDR.KwargsFromString(dev_args))
    sdr.setAntenna(SoapySDR.SOAPY_SDR_RX, 0, antenna)
    sdr.setGainMode(SoapySDR.SOAPY_SDR_RX, 0, False)   # disable AGC
    sdr.setGain(SoapySDR.SOAPY_SDR_RX, 0, float(gain))
    sdr.setSampleRate(SoapySDR.SOAPY_SDR_RX, 0, SAMP_RATE)

    # Remote dx-R2: force IQ onto lossless TCP (ignored on a local open).
    stream_args = {"remote:prot": "tcp"} if "remote" in dev_args else {}
    rxStream = sdr.setupStream(SoapySDR.SOAPY_SDR_RX, SoapySDR.SOAPY_SDR_CF32, [0], stream_args)
    sdr.activateStream(rxStream)

    buf_len    = FFT_SIZE
    buf        = np.zeros(buf_len, dtype=np.complex64)
    settle_n   = max(1, int(SAMP_RATE * settle_s / buf_len))
    dwell_n    = max(4, int(SAMP_RATE * dwell_s  / buf_len))

    measurements = {}   # freq_hz → list of per-hop dB readings
    chan_list = list(channels())
    t0 = time.time()

    # Build list of hop center frequencies covering the AM band
    hops = []
    center = FIRST_CHAN_HZ + int(half_bw)
    while center - half_bw <= LAST_CHAN_HZ:
        hops.append(center)
        center += hop_step

    print(f"[scan] {len(hops)} hops to cover AM band, "
          f"{dwell_n} FFT windows per hop", file=sys.stderr)

    try:
        for hop_i, hop_freq in enumerate(hops):
            print(f"[scan] hop {hop_i+1}/{len(hops)}: "
                  f"{hop_freq/1e6:.3f} MHz center, "
                  f"{int(time.time()-t0)}s elapsed", file=sys.stderr)
            sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, float(hop_freq))

            # drain settle period
            for _ in range(settle_n):
                sdr.readStream(rxStream, [buf], buf_len,
                               timeoutUs=int(settle_s * 2e6))

            # accumulate power spectra across dwell_n FFT windows. SoapyRemote
            # delivers PARTIAL reads (~1006 samples/MTU datagram), so a single
            # readStream rarely fills FFT_SIZE — fill a full block across multiple
            # reads before transforming. (The local Pi returned full blocks; the
            # remote does not — the same partial-read gotcha that bit rx_fm/wbfm.
            # The old `r.ret == buf_len` check was never true over the network →
            # zero measurements.)
            acc = np.zeros(FFT_SIZE)
            n_good = 0
            fill = 0
            for _ in range(dwell_n * 8):
                r = sdr.readStream(rxStream, [buf[fill:]], buf_len - fill,
                                   timeoutUs=int(dwell_s * 2e6))
                if r.ret > 0:
                    fill += r.ret
                    if fill >= buf_len:
                        acc += fft_power_db(buf)
                        n_good += 1
                        fill = 0
                        if n_good >= dwell_n:
                            break

            if n_good == 0:
                continue
            avg_spectrum = acc / n_good

            # map each AM channel to its nearest FFT bin
            for freq_hz in chan_list:
                offset = freq_hz - hop_freq
                if abs(offset) > half_bw * 0.9:   # skip near edges
                    continue
                bin_idx = int(round(offset / bin_hz)) + FFT_SIZE // 2
                bin_idx = max(0, min(FFT_SIZE - 1, bin_idx))
                measurements.setdefault(freq_hz, []).append(avg_spectrum[bin_idx])
    finally:
        sdr.deactivateStream(rxStream)
        sdr.closeStream(rxStream)

    elapsed = int(time.time() - t0)
    print(f"[scan] done in {elapsed}s", file=sys.stderr)
    return {f: float(np.mean(v)) for f, v in measurements.items() if v}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gain",         default=40,  type=float)
    ap.add_argument("--threshold-db", default=10,  type=float)
    ap.add_argument("--settle-ms",    default=300, type=int)
    ap.add_argument("--dwell-ms",     default=500, type=int)
    ap.add_argument("--antennas", default="Antenna C",
                    help="comma-separated antenna ports to sweep (AM is normally the "
                         "long-wire on Antenna C). >1 records the best antenna per station.")
    ap.add_argument("--out", default="/var/lib/sdr-streams/stations_am.json")
    args = ap.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    antennas = [a.strip() for a in args.antennas.split(",") if a.strip()]
    print(f"[scan] AM scan: {FIRST_CHAN_HZ//1000}–{LAST_CHAN_HZ//1000} kHz, gain {args.gain}, "
          f"antennas={antennas}", file=sys.stderr)

    per_ant = {}   # antenna -> {freq_hz: snr_db}
    for ant in antennas:
        print(f"[scan] sweeping AM on {ant!r}", file=sys.stderr)
        meas = measure_band(args.gain, args.settle_ms, args.dwell_ms, antenna=ant)
        if not meas:
            print(f"[scan] {ant}: no measurements", file=sys.stderr)
            continue
        noise = statistics.median(meas.values())
        print(f"[scan] {ant}: noise floor ~{noise:.1f} dB", file=sys.stderr)
        per_ant[ant] = {f: db - noise for f, db in meas.items()}

    if not per_ant:
        print("[scan] no measurements collected on any antenna", file=sys.stderr)
        sys.exit(1)

    stations = []
    for freq_hz in channels():
        by_ant, best_ant, best_snr = {}, None, -1e9
        for ant in antennas:
            snr = per_ant.get(ant, {}).get(freq_hz)
            if snr is None:
                continue
            by_ant[ant.split()[-1]] = round(snr, 1)
            if snr > best_snr:
                best_snr, best_ant = snr, ant
        if best_ant is None or best_snr < args.threshold_db:
            continue
        entry = {"freq_khz": freq_hz // 1000, "snr_db": round(best_snr, 1), "antenna": best_ant}
        if len(antennas) > 1:
            entry["by_antenna"] = by_ant
        stations.append(entry)

    stations.sort(key=lambda s: -s["snr_db"])
    out = {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "antennas":   antennas,
        "stations":   stations,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"[scan] wrote {len(stations)} stations ({len(antennas)} antenna(s)) to {args.out}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
