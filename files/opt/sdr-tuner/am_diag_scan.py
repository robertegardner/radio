#!/usr/bin/env python3
"""
am_diag_scan.py — diagnostic MW broadcast band scan via the dx-R2.

Sweeps 500-1700 kHz with the same SDR configuration that am_stream.py uses
in live AM tuning (HDR on, DAB notch on, RF notch off, biasT off, Antenna C,
fixed manual gain, AGC off). The point is to see what the antenna is
actually delivering through the chain we're debugging — not a stitched-
together scan from a different configuration.

Single LO position at the band center (~1100 kHz). 2 MHz sample rate gives
us ±1 MHz of coverage which spans the entire MW broadcast band in one shot.
PSD via Welch's method, averaged per second, saved to CSV plus a summary
report at the end.

Args:
  --duration SECONDS  scan length (default 1800 = 30 min)
  --rfnotch on|off    override rfnotch_ctrl (default off, matching am_stream.py)
  --out PATH          CSV output path
"""
import argparse
import csv
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_CS16, SOAPY_SDR_RX

HW_RATE = 2_000_000
# Off-center by 0.5 kHz so no real broadcast carrier (10 kHz grid) lands
# exactly on the LO and gets eaten by residual DC suppression.
LO_FREQ = 1_100_500
N_FFT = 1024  # bin width = 2e6 / 1024 ≈ 1953 Hz (in 2-3 kHz target range)
READ_BLOCK = N_FFT * 16  # samples per readStream call
ANTENNA = "Antenna C"
DRIVER = "sdrplay"
GAIN = 20.0  # matches am_stream.py

# Front-end settings written explicitly. Each notch is independently controlled
# by a CLI flag so we can run controlled A/B experiments. biasT off (passive
# longwire), HDR on (matching live AM listening).
BASE_SETTINGS = [
    ("hdr_ctrl", "true"),
    ("biasT_ctrl", "false"),
]

# Frequencies the report calls out explicitly. Each entry: (kHz, label).
TARGET_FREQS = [
    (960, "KSIM local (Sikeston)"),
    (1120, "KMOX St. Louis 50 kW (skywave-only by day)"),
    (1220, "KGIR / co-channel local"),
    (1230, "KZYM Cape Girardeau local"),
    (1380, "(probe)"),
    (555, "below-band noise reference"),
    (1665, "above-band noise reference"),
]


def open_sdr(rfnotch: str, dabnotch: str, extra_settings: list[tuple[str, str]]):
    settings = list(BASE_SETTINGS)
    settings.append(("rfnotch_ctrl", "true" if rfnotch == "on" else "false"))
    settings.append(("dabnotch_ctrl", "true" if dabnotch == "on" else "false"))
    for k, v in extra_settings:
        settings.append((k, v))

    sdr = SoapySDR.Device(f"driver={DRIVER}")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, HW_RATE)
    sdr.setAntenna(SOAPY_SDR_RX, 0, ANTENNA)
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception as e:
        sys.stderr.write(f"am_diag_scan: setGainMode(False) failed: {e}\n")
    sdr.setGain(SOAPY_SDR_RX, 0, GAIN)
    sdr.setFrequency(SOAPY_SDR_RX, 0, LO_FREQ)
    for key, want in settings:
        try:
            sdr.writeSetting(key, want)
        except Exception as e:
            sys.stderr.write(f"am_diag_scan: writeSetting({key}={want}) failed: {e}\n")
    return sdr, settings


def dump_state(sdr, settings, state_path: Path | None = None):
    """Mirror am_stream.py's startup log so we have an apples-to-apples record.
    Writes to stdout AND, if state_path given, to that file. The state file is
    the durable record that lives next to the scan CSV."""
    lines: list[str] = []
    def emit(s: str) -> None:
        lines.append(s)
        print(s)

    emit("am_diag_scan: ---- SDR state ----")
    emit(f"am_diag_scan: driver={sdr.getDriverKey()} hw={sdr.getHardwareKey()}")
    emit(
        f"am_diag_scan: antenna={sdr.getAntenna(SOAPY_SDR_RX, 0)!r} "
        f"sample_rate={sdr.getSampleRate(SOAPY_SDR_RX, 0):.0f} "
        f"freq={sdr.getFrequency(SOAPY_SDR_RX, 0):.0f} "
        f"bw={sdr.getBandwidth(SOAPY_SDR_RX, 0):.0f}"
    )
    try:
        agc = sdr.getGainMode(SOAPY_SDR_RX, 0)
    except Exception as e:
        agc = f"<err {e}>"
    emit(f"am_diag_scan: total_gain={sdr.getGain(SOAPY_SDR_RX, 0):.2f} agc_mode={agc}")
    emit("am_diag_scan: --- antennas exposed ---")
    for ant in sdr.listAntennas(SOAPY_SDR_RX, 0):
        emit(f"am_diag_scan:   antenna={ant!r}")
    emit("am_diag_scan: --- gain elements ---")
    for elem in sdr.listGains(SOAPY_SDR_RX, 0):
        try:
            g = sdr.getGain(SOAPY_SDR_RX, 0, elem)
            r = sdr.getGainRange(SOAPY_SDR_RX, 0, elem)
            emit(f"am_diag_scan:   gain[{elem}]={g:.2f} dB (range [{r.minimum()}, {r.maximum()}])")
        except Exception as e:
            emit(f"am_diag_scan:   gain[{elem}]: <err {e}>")
    emit("am_diag_scan: --- getSettingInfo() (FULL) ---")
    for info in sdr.getSettingInfo():
        key = info.key
        try:
            val = sdr.readSetting(key)
        except Exception as e:
            val = f"<err {e}>"
        # Dump every metadata field we can pull off SoapySDR.ArgInfo so
        # we can see the driver's own description of each knob.
        opts = list(info.options) if info.options else []
        opt_names = list(info.optionNames) if info.optionNames else []
        rng = ""
        try:
            r = info.range
            rng = f" range=[{r.minimum()},{r.maximum()},step={r.step()}]"
        except Exception:
            pass
        emit(
            f"am_diag_scan:   setting[{key}] name={info.name!r} type={info.type} "
            f"default={info.value!r} current={val!r} desc={info.description!r}"
            f"{rng}"
            + (f" options={opts}" if opts else "")
            + (f" optionNames={opt_names}" if opt_names else "")
        )
    emit("am_diag_scan: --- verifying expected ---")
    for key, want in settings:
        try:
            got = sdr.readSetting(key)
        except Exception as e:
            got = f"<err {e}>"
        mark = "ok" if got == want else "MISMATCH"
        emit(f"am_diag_scan: {key}={got!r} (wanted {want!r}) [{mark}]")
    emit("am_diag_scan: -------------------")
    sys.stdout.flush()
    if state_path is not None:
        state_path.write_text("\n".join(lines) + "\n")


def run_scan(
    duration_s: float,
    rfnotch: str,
    dabnotch: str,
    extra_settings: list[tuple[str, str]],
    csv_path: Path,
    state_path: Path,
):
    sdr, settings = open_sdr(rfnotch, dabnotch, extra_settings)
    dump_state(sdr, settings, state_path)

    rx = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    sdr.activateStream(rx)

    # Frequency axis. fftshift puts DC at center; offset to absolute frequency.
    freqs_hz = np.fft.fftshift(np.fft.fftfreq(N_FFT, 1.0 / HW_RATE)) + LO_FREQ
    band_mask = (freqs_hz >= 450_000) & (freqs_hz <= 1_750_000)
    band_freqs = freqs_hz[band_mask]
    win = np.hanning(N_FFT).astype(np.float32)
    win_pow = float((win * win).sum())

    raw = np.empty(READ_BLOCK * 2, dtype=np.int16)
    sample_acc = np.empty(0, dtype=np.complex64)
    sec_accum = np.zeros(N_FFT, dtype=np.float64)
    sec_count = 0

    psd_log = []  # (elapsed_s, psd_db_in_band)
    t0 = time.monotonic()
    deadline = t0 + duration_s
    next_emit = t0 + 1.0

    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    last_status_min = -1
    while running and time.monotonic() < deadline:
        sr = sdr.readStream(rx, [raw], READ_BLOCK, timeoutUs=500_000)
        if sr.ret <= 0:
            continue
        n = sr.ret
        i = raw[0:2 * n:2].astype(np.float32) / 32768.0
        q = raw[1:2 * n:2].astype(np.float32) / 32768.0
        iq = (i + 1j * q).astype(np.complex64)
        if len(sample_acc):
            iq = np.concatenate((sample_acc, iq))

        n_chunks = len(iq) // N_FFT
        for c in range(n_chunks):
            chunk = iq[c * N_FFT:(c + 1) * N_FFT]
            spec = np.fft.fft(chunk * win)
            sec_accum += np.abs(spec) ** 2 / win_pow
            sec_count += 1
        sample_acc = iq[n_chunks * N_FFT:]

        now = time.monotonic()
        if now >= next_emit and sec_count > 0:
            avg = sec_accum / sec_count
            psd_shifted = np.fft.fftshift(avg)
            psd_band = psd_shifted[band_mask]
            psd_db = 10.0 * np.log10(psd_band + 1e-20)
            psd_log.append((now - t0, psd_db))
            sec_accum[:] = 0.0
            sec_count = 0
            next_emit = now + 1.0
            cur_min = int((now - t0) // 60)
            if cur_min != last_status_min:
                last_status_min = cur_min
                remaining = int(deadline - now)
                print(f"am_diag_scan: t+{cur_min:02d}m, {remaining}s remaining", flush=True)

    try:
        sdr.deactivateStream(rx)
        sdr.closeStream(rx)
    except Exception:
        pass

    # Write CSV
    print(f"am_diag_scan: writing {csv_path}", flush=True)
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        header = ["time_s"] + [f"{fr / 1e3:.2f}kHz" for fr in band_freqs]
        w.writerow(header)
        for t, psd_db in psd_log:
            w.writerow([f"{t:.1f}"] + [f"{x:.2f}" for x in psd_db])

    return band_freqs, psd_log


def summarize(band_freqs: np.ndarray, psd_log, label: str):
    if not psd_log:
        print("am_diag_scan: no PSD samples captured, nothing to summarize")
        return
    psd = np.array([p[1] for p in psd_log])  # (T, F)
    peak = psd.max(axis=0)
    median = np.median(psd, axis=0)

    print("\n" + "=" * 72)
    print(f"am_diag_scan summary  ({len(psd_log)} per-second PSD frames, {label})")
    print("=" * 72)

    print("\n--- Top 30 bins by peak signal level ---")
    print(f"{'freq_kHz':>10}  {'peak_dB':>9}  {'median_dB':>11}  {'p-m_dB':>8}")
    order = np.argsort(peak)[::-1][:30]
    for idx in order:
        f_khz = band_freqs[idx] / 1000.0
        print(f"{f_khz:10.2f}  {peak[idx]:9.2f}  {median[idx]:11.2f}  {peak[idx]-median[idx]:8.2f}")

    print("\n--- Named target frequencies ---")
    for tgt_khz, label in TARGET_FREQS:
        idx = int(np.argmin(np.abs(band_freqs - tgt_khz * 1000)))
        actual = band_freqs[idx] / 1000.0
        print(
            f"  {tgt_khz:>4} kHz [{label}]: nearest bin {actual:7.2f}kHz  "
            f"peak={peak[idx]:7.2f}dB  median={median[idx]:7.2f}dB  p-m={peak[idx]-median[idx]:6.2f}dB"
        )

    # Noise floor: median across all bins in "empty" stretches (no AM channel)
    # AM US channels are on 10 kHz grid: 540, 550, ..., 1700. Bins between
    # those (e.g. 545, 555, ..., 1695) are channel-free.
    empty_centers_khz = list(range(545, 1700, 10))
    empty_idxs = [int(np.argmin(np.abs(band_freqs - c * 1000))) for c in empty_centers_khz]
    empty_medians = median[empty_idxs]
    print("\n--- Noise floor (median PSD of off-grid bins) ---")
    print(f"  N={len(empty_idxs)} off-grid bins between 545–1695 kHz")
    print(f"  median: {np.median(empty_medians):.2f} dB")
    print(f"  mean:   {np.mean(empty_medians):.2f} dB")
    print(f"  min:    {np.min(empty_medians):.2f} dB  @ {band_freqs[empty_idxs[int(np.argmin(empty_medians))]]/1000:.2f} kHz")
    print(f"  max:    {np.max(empty_medians):.2f} dB  @ {band_freqs[empty_idxs[int(np.argmax(empty_medians))]]/1000:.2f} kHz")

    # Discrete spikes in the noise floor → candidate Pi-generated digital RFI
    # Anything >10 dB above the global noise-floor median is flagged.
    nf_median = float(np.median(empty_medians))
    spikes = []
    for i, c_khz in enumerate(empty_centers_khz):
        idx = empty_idxs[i]
        if median[idx] > nf_median + 10.0:
            spikes.append((c_khz, median[idx]))
    if spikes:
        print(f"\n--- Off-grid bins >10 dB above noise floor ({nf_median:.2f} dB) ---")
        for f_khz, m_db in sorted(spikes, key=lambda x: -x[1]):
            print(f"  {f_khz:>5} kHz: median {m_db:.2f} dB ({m_db - nf_median:+.2f} above NF)")
    else:
        print("\n--- No off-grid bins >10 dB above noise floor ---")

    # 960 kHz width check: walk ±20 kHz, report peak in each bin
    print("\n--- 940–980 kHz neighborhood (looking for the wide-carrier artifact) ---")
    around = (band_freqs >= 940_000) & (band_freqs <= 980_000)
    around_idx = np.where(around)[0]
    for idx in around_idx:
        f_khz = band_freqs[idx] / 1000.0
        delta_from_nf = median[idx] - nf_median
        marker = " <-- 960 kHz" if abs(f_khz - 960) < 1.0 else ""
        print(f"  {f_khz:7.2f} kHz  peak={peak[idx]:7.2f}dB  median={median[idx]:7.2f}dB  med-NF={delta_from_nf:+6.2f}dB{marker}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--duration", type=float, default=1800.0)
    ap.add_argument("--rfnotch", choices=["on", "off"], default="off")
    ap.add_argument("--dabnotch", choices=["on", "off"], default="off")
    ap.add_argument("--set", action="append", default=[],
                    help="extra setting as key=value (repeatable), e.g. --set amnotch_ctrl=true")
    ap.add_argument("--tag", default="",
                    help="short tag to embed in output filenames")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    extra: list[tuple[str, str]] = []
    for s in args.set:
        if "=" not in s:
            print(f"am_diag_scan: ignoring malformed --set {s!r}", file=sys.stderr)
            continue
        k, v = s.split("=", 1)
        extra.append((k.strip(), v.strip()))

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    tag_part = f"-{args.tag}" if args.tag else ""
    diag_dir = Path("/var/lib/sdr-streams/diag")
    diag_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out or (diag_dir / f"am-scan-{timestamp}{tag_part}.csv")
    report_path = csv_path.with_suffix(".report.txt")
    state_path = csv_path.with_suffix(".state.txt")

    label = f"rfnotch={args.rfnotch} dabnotch={args.dabnotch}"
    if extra:
        label += " " + " ".join(f"{k}={v}" for k, v in extra)
    print(f"am_diag_scan: duration={args.duration:.0f}s {label}")
    print(f"am_diag_scan: csv={csv_path}")
    print(f"am_diag_scan: report={report_path}")
    print(f"am_diag_scan: state={state_path}")
    sys.stdout.flush()

    band_freqs, psd_log = run_scan(
        args.duration, args.rfnotch, args.dabnotch, extra, csv_path, state_path
    )

    # Tee summary to both stdout and a report file.
    import io
    buf = io.StringIO()
    real_stdout = sys.stdout
    class Tee:
        def write(self, s):
            real_stdout.write(s); buf.write(s)
        def flush(self):
            real_stdout.flush()
    sys.stdout = Tee()
    try:
        summarize(band_freqs, psd_log, label)
    finally:
        sys.stdout = real_stdout
    report_path.write_text(buf.getvalue())
    print(f"\nam_diag_scan: report saved to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
