#!/usr/bin/env python3
"""
am_stream.py — AM demodulator with a proper narrow channel filter.

Replaces rx_fm for AM/NFM modes. Background: rx_fm under SoapySDR silently
ignores its -r (audio rate) flag, which means the AM envelope detector runs
across the entire hardware IF bandwidth (~2 MHz on the dx-R2). With no
channel filter, the strongest station anywhere in the IF dominates the
audio output regardless of where you "tune."

This script reads raw IQ from the dx-R2 via SoapySDR and runs a real DSP
chain:

  1. Engage the dx-R2 MW-band features (HDR + DAB notch). HDR provides a
     dedicated wide-DR signal path centered on MW and notably eliminates the
     DC spike that direct-conversion architectures normally produce, so we
     can place the target carrier at DC instead of using an LO offset.
  2. Hardware LO is set directly to FREQ.
  3. Two-stage decimating FIR filter narrows to ~6 kHz around DC:
       2 MHz --(decim 8)--> 250 kHz --(decim 5)--> 50 kHz
  4. FFT-based one-shot PLL lock finds the actual carrier offset (carrier
     tolerance + LO drift typically ±50 Hz of DC).
  5. Per-sample NCO de-rotates the carrier exactly to DC; real part is audio.
  6. Per-sample EMA tracks the carrier amplitude (slow); audio is normalized
     by it for modulation-index output that's invariant to slow fading.
  7. s16le mono PCM at 50 kHz to stdout for ffmpeg.

Reads FREQ and GAIN from /etc/sdr-streams/active.env.
"""
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_CS16, SOAPY_SDR_RX

RFI_STATUS_PATH = Path("/run/sdr-streams/rfi_status.json")
SRC_ENV = Path("/etc/radio-compute/source-dx-r2.env")

HW_RATE = 2_000_000
# With HDR mode engaged the dx-R2's HDR signal path eliminates the DC spike
# that motivated the +500 kHz offset on the non-HDR path. Place the target
# at DC and let the PLL FFT search take up the small carrier-tolerance slack.
LO_OFFSET = 0
ANTENNA = "Antenna C"
DRIVER = "sdrplay"

# Settings we engage for MW (freq < 30 MHz). hdr_ctrl gives the dx-R2 a
# dedicated wide-DR signal path centered on MW; dabnotch_ctrl rejects the
# DAB band (no downside in the US). We deliberately leave rfnotch_ctrl OFF —
# on the dx-R2 it's a combined MW+FM broadcast notch and would attenuate the
# band we want to listen to. biasT_ctrl off because Antenna C is a passive
# long-wire and we don't want DC out the SMA.
MW_SETTINGS = (
    ("hdr_ctrl", "true"),
    ("dabnotch_ctrl", "true"),
    ("biasT_ctrl", "false"),
)

DECIM1 = 8
DECIM2 = 5
OUT_RATE = HW_RATE // (DECIM1 * DECIM2)  # 50_000

# Block sized so total decimation (40) divides cleanly — no per-block rate drift.
BLOCK_COMPLEX = 32_000


def lowpass_taps(num_taps: int, cutoff: float, fs: float, beta: float = 0.0) -> np.ndarray:
    n = np.arange(num_taps) - (num_taps - 1) / 2
    window = np.kaiser(num_taps, beta) if beta > 0 else np.hamming(num_taps)
    h = np.sinc(2 * cutoff / fs * n) * window
    return (h / h.sum()).astype(np.float32)


# Stage 1: 2 MHz input, decim 8, output 250 kHz. Cutoff 100 kHz keeps anti-alias margin.
TAPS1 = lowpass_taps(63, 100_000, HW_RATE)
# Stage 2: 250 kHz input, decim 5, output 50 kHz. Channel filter that isolates one
# AM station. Empirically on KMOX 1120 there's a ~6.7 kHz tone pulsing at ~10 Hz
# (likely a station audio-processor artifact or sync subcarrier) that needs to be
# rejected hard — it sounds like discrete beeps. A loose filter (e.g. 255 taps
# Kaiser β=8.6 at 4.5 kHz cutoff) is only -50 dB at 6766 Hz, plenty audible.
# 511 taps with Kaiser β=10 and a 3.5 kHz cutoff puts that tone -105 dB down,
# fully inaudible, while leaving voice content (1-3 kHz) intact. The tighter
# cutoff also drops the 7-9 kHz noise floor by ~8 dB, which reduces hiss on
# weaker stations like KZYM 1220.
TAPS2 = lowpass_taps(511, 3_500, HW_RATE // DECIM1, beta=10.0)


def device_args() -> str:
    """SoapySDR device args. On the rack (radio-compute) the dx-R2 is REMOTE —
    read SOAPY_ARGS (driver=remote,...,remote:driver=sdrplay) from the source env.
    On the Pi (file absent) fall back to the local driver=sdrplay. One script,
    both tiers — same pattern as fm_scan/am_scan."""
    if SRC_ENV.exists():
        for line in SRC_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("SOAPY_ARGS="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return f"driver={DRIVER}"


def read_env(path: str = "/etc/sdr-streams/active.env") -> dict:
    env: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def parse_freq(s: str) -> float:
    s = s.strip()
    mult = 1.0
    if s[-1] in "kK":
        mult, s = 1_000.0, s[:-1]
    elif s[-1] in "mM":
        mult, s = 1_000_000.0, s[:-1]
    return float(s) * mult


def conv_decim(x: np.ndarray, taps: np.ndarray, decim: int, hist: np.ndarray):
    """Overlap-save FIR + decimation. Returns (output, new_hist)."""
    extended = np.concatenate((hist, x))
    y = np.convolve(extended, taps, mode="valid").astype(np.complex64)
    new_hist = x[-(len(taps) - 1):].copy()
    return y[::decim], new_hist


def startup_rfi_scan(sdr, rx, lo_freq_hz: float, target_freq_hz: float,
                     duration_s: float = 5.0) -> None:
    """Pre-streaming PSD measurement of the captured ±1 MHz band.

    Logs the noise floor (median PSD of off-grid bins between AM 10 kHz
    channels) and any off-grid bins more than 15 dB above that floor —
    those are likely local RFI sources. Also logs the SNR at the tuned
    station. Writes a JSON snapshot to RFI_STATUS_PATH for the admin UI
    banner. Costs ~duration_s of startup latency but the journal record
    of each restart's noise environment lets us spot RFI degradation
    over time.
    """
    n_fft = 1024
    read_block = n_fft * 16
    raw = np.empty(read_block * 2, dtype=np.int16)
    accum = np.zeros(n_fft, dtype=np.float64)
    count = 0
    win = np.hanning(n_fft).astype(np.float32)
    win_pow = float((win * win).sum())

    deadline = time.monotonic() + duration_s
    sys.stderr.write(f"am_stream: noise-floor scan ({duration_s:.0f}s)…\n")
    sys.stderr.flush()
    # SoapyRemote delivers PARTIAL reads (~1006 samples/MTU datagram), almost
    # always < n_fft, so a per-read `len//n_fft` chunking yields ZERO FFT blocks
    # over the network (same gotcha that bit am_scan/rx_fm). Accumulate across
    # reads and consume whole n_fft blocks from the buffer.
    acc = np.empty(0, dtype=np.complex64)
    while time.monotonic() < deadline:
        sr = sdr.readStream(rx, [raw], read_block, timeoutUs=500_000)
        if sr.ret <= 0:
            continue
        n = sr.ret
        i = raw[0:2 * n:2].astype(np.float32) / 32768.0
        q = raw[1:2 * n:2].astype(np.float32) / 32768.0
        iq = (i + 1j * q).astype(np.complex64)
        acc = np.concatenate((acc, iq)) if acc.size else iq
        while acc.size >= n_fft:
            chunk = acc[:n_fft]
            acc = acc[n_fft:]
            spec = np.fft.fft(chunk * win)
            accum += np.abs(spec) ** 2 / win_pow
            count += 1
    if count == 0:
        sys.stderr.write("am_stream: noise-floor scan got no samples, skipping\n")
        sys.stderr.flush()
        return

    avg = accum / count
    psd_shifted = np.fft.fftshift(avg)
    freqs_hz = np.fft.fftshift(np.fft.fftfreq(n_fft, 1.0 / HW_RATE)) + lo_freq_hz
    psd_db = 10.0 * np.log10(psd_shifted + 1e-20)

    # Off-grid AM bin centers (between 10 kHz US channels). Take only those
    # actually covered by the captured band.
    band_lo = float(freqs_hz.min())
    band_hi = float(freqs_hz.max())
    off_grid: list[tuple[int, int]] = []
    for c_khz in range(545, 1700, 10):
        c_hz = c_khz * 1000.0
        if c_hz < band_lo or c_hz > band_hi:
            continue
        idx = int(np.argmin(np.abs(freqs_hz - c_hz)))
        off_grid.append((c_khz, idx))
    if not off_grid:
        sys.stderr.write("am_stream: no off-grid AM bins in captured band, skipping\n")
        sys.stderr.flush()
        return

    levels = np.array([psd_db[i] for _, i in off_grid])
    nf_median = float(np.median(levels))

    rfi: list[dict] = []
    for c_khz, idx in off_grid:
        lvl = float(psd_db[idx])
        if lvl > nf_median + 15.0:
            rfi.append({
                "freq_khz": c_khz,
                "level_db": round(lvl, 2),
                "above_nf_db": round(lvl - nf_median, 2),
            })
    rfi.sort(key=lambda r: -r["above_nf_db"])

    target_idx = int(np.argmin(np.abs(freqs_hz - target_freq_hz)))
    station_level = float(psd_db[target_idx])
    station_snr = station_level - nf_median

    sys.stderr.write(
        f"am_stream: noise_floor_db={nf_median:.2f} "
        f"station_level_db={station_level:.2f} station_snr_db={station_snr:.2f}\n"
    )
    if rfi:
        sys.stderr.write(f"am_stream: {len(rfi)} RFI candidate(s) >15 dB above NF:\n")
        for r in rfi[:6]:
            sys.stderr.write(
                f"am_stream:   {r['freq_khz']:>4} kHz: "
                f"{r['level_db']:+.2f} dB (+{r['above_nf_db']:.2f} above NF)\n"
            )
    else:
        sys.stderr.write("am_stream: no RFI candidates >15 dB above NF\n")
    sys.stderr.flush()

    status = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "tuned_freq_khz": int(round(target_freq_hz / 1000)),
        "noise_floor_db": round(nf_median, 2),
        "station_level_db": round(station_level, 2),
        "station_snr_db": round(station_snr, 2),
        "rfi_candidates": rfi,
    }
    try:
        RFI_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = RFI_STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2))
        tmp.replace(RFI_STATUS_PATH)
    except OSError as e:
        sys.stderr.write(f"am_stream: could not write {RFI_STATUS_PATH}: {e}\n")
        sys.stderr.flush()


def main() -> int:
    env = read_env()
    target_freq = parse_freq(env["FREQ"])
    gain = float(env.get("GAIN", 30))
    lo_freq = target_freq + LO_OFFSET
    antenna = env.get("ANTENNA") or ANTENNA

    dev_args = device_args()
    remote = "remote" in dev_args
    sys.stderr.write(
        f"am_stream: target={target_freq/1e3:.1f}kHz LO={lo_freq/1e3:.1f}kHz "
        f"gain={gain} ant={antenna!r} OUT_RATE={OUT_RATE} dev={dev_args!r}\n"
    )
    sys.stderr.flush()

    sdr = SoapySDR.Device(SoapySDR.KwargsFromString(dev_args))
    sdr.setSampleRate(SOAPY_SDR_RX, 0, HW_RATE)
    sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    # Disable the dx-R2's hardware AGC. With both hardware AGC and our software
    # AGC active, the two control loops fight: hardware AGC compresses the IF in
    # ~10ms steps, software AGC reacts to the moving envelope, you get 20-30 dB
    # of block-rate pumping that sounds like static + buzz. A fixed manual gain
    # gives a stable envelope for the software AGC to ride.
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception as e:
        sys.stderr.write(f"am_stream: setGainMode(False) failed: {e}\n")
    sdr.setGain(SOAPY_SDR_RX, 0, gain)
    sdr.setFrequency(SOAPY_SDR_RX, 0, lo_freq)

    # Engage MW-band features on the dx-R2. The SoapySDRPlay3 driver silently
    # no-ops unknown keys, so read back each value to confirm. This is the only
    # way to catch driver-version mismatches at startup.
    if target_freq < 30_000_000:
        for key, want in MW_SETTINGS:
            sdr.writeSetting(key, want)
            got = sdr.readSetting(key)
            mark = "ok" if got == want else "MISMATCH"
            sys.stderr.write(f"am_stream: {key}={got!r} (wanted {want!r}) [{mark}]\n")

    # Full SDR state dump. Anything the driver exposes via getSettingInfo() that
    # we didn't explicitly write — log its current value so we can see defaults
    # that may be biting us. Also dump achieved rate/freq/antenna/gain (driver
    # may quantize/clamp what we asked for) and every per-element gain stage.
    sys.stderr.write("am_stream: ---- SDR state ----\n")
    sys.stderr.write(f"am_stream: driver={sdr.getDriverKey()} hw={sdr.getHardwareKey()}\n")
    sys.stderr.write(
        f"am_stream: antenna={sdr.getAntenna(SOAPY_SDR_RX, 0)!r} "
        f"sample_rate={sdr.getSampleRate(SOAPY_SDR_RX, 0):.0f} "
        f"freq={sdr.getFrequency(SOAPY_SDR_RX, 0):.0f} "
        f"bw={sdr.getBandwidth(SOAPY_SDR_RX, 0):.0f}\n"
    )
    try:
        agc_mode = sdr.getGainMode(SOAPY_SDR_RX, 0)
    except Exception as e:
        agc_mode = f"<err {e}>"
    sys.stderr.write(
        f"am_stream: total_gain={sdr.getGain(SOAPY_SDR_RX, 0):.2f} agc_mode={agc_mode}\n"
    )
    for elem in sdr.listGains(SOAPY_SDR_RX, 0):
        try:
            g = sdr.getGain(SOAPY_SDR_RX, 0, elem)
            r = sdr.getGainRange(SOAPY_SDR_RX, 0, elem)
            sys.stderr.write(
                f"am_stream:   gain[{elem}]={g:.2f} dB (range [{r.minimum()}, {r.maximum()}])\n"
            )
        except Exception as e:
            sys.stderr.write(f"am_stream:   gain[{elem}]: <err {e}>\n")
    for info in sdr.getSettingInfo():
        key = info.key
        try:
            val = sdr.readSetting(key)
        except Exception as e:
            val = f"<err {e}>"
        sys.stderr.write(f"am_stream:   setting[{key}]={val!r} default={info.value!r}\n")
    sys.stderr.write("am_stream: -------------------\n")
    sys.stderr.flush()

    # Remote dx-R2: force the IQ onto lossless TCP (SoapyRemote's UDP firehose
    # drops datagrams → demod artifacts). Harmless/ignored on a local open.
    stream_args = {"remote:prot": "tcp"} if remote else {}
    rx = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16, [0], stream_args)
    sdr.activateStream(rx)

    # Pre-streaming RFI scan. ~5 s of latency before audio starts, but each
    # restart fingerprints the noise environment in the journal and feeds the
    # admin-UI RFI banner. Skipped for non-MW tunes (out-of-band coverage).
    if target_freq < 30_000_000:
        startup_rfi_scan(sdr, rx, lo_freq, target_freq)

    raw = np.empty(BLOCK_COMPLEX * 2, dtype=np.int16)

    # NCO mix-back: this becomes identity (omega=0) when LO_OFFSET=0, i.e.
    # when HDR is engaged and we place the target at DC directly. Kept in
    # general form so this file can fall back to LO_OFFSET>0 if HDR ever
    # gets disabled (e.g. for HF outside the MW band).
    nco_omega = +2.0 * np.pi * LO_OFFSET / HW_RATE
    sample_n = 0

    hist1 = np.zeros(len(TAPS1) - 1, dtype=np.complex64)
    hist2 = np.zeros(len(TAPS2) - 1, dtype=np.complex64)

    # Accumulator so we always process exactly BLOCK_COMPLEX samples per pass.
    # SoapySDR readStream may return fewer than requested; processing variable
    # block sizes would drift the decimation phase and produce clicks.
    accum = np.empty(0, dtype=np.complex64)

    # Synchronous AM demodulation via FFT-locked NCO.
    #
    # The carrier doesn't land exactly at DC after the channel filter — it sits
    # at some small offset (KMOX ~10 Hz, others up to ±50 Hz due to TX tolerance
    # + dx-R2 LO drift). A naïve "complex DC = carrier" assumption averages a
    # rotating signal to zero and breaks normalization. Instead:
    #   1. Collect ~0.5 s of post-filter IQ, FFT, find carrier peak within ±200 Hz
    #   2. Set NCO frequency = -offset, mix y2 × conj(NCO) → carrier truly at DC
    #   3. Take real part as audio (linear demod, no envelope-detection harmonics)
    #   4. Per-sample EMA tracks slow C(t); audio = real_part − C; normalize by C
    nco_freq_hz = 0.0
    nco_phase = 0.0
    fft_acc = []
    fft_locked = False
    fft_target_samples = OUT_RATE // 2  # 0.5 sec of post-filter data
    sample_period = 1.0 / OUT_RATE

    # Block-rate normalization (the e45da31 version). The per-sample dual EMA
    # from fab5b08 was reverted here during 2026-05-27 bisection, when the
    # real root cause of "AM illegible" turned out to be local RFI swamping
    # the noise floor (see /var/lib/sdr-streams/diag/am-debug-summary-20260527.md),
    # not the normalization. Reintroducing per-sample EMA is a separate
    # decision that should be made after the RFI environment is cleaned up.
    sig_amp = np.float32(0.01)
    sig_alpha = np.float32(0.05)
    audio_dc = np.float32(0.0)
    SIG_FLOOR = np.float32(0.001)
    # 0.7 is loud enough that lightly-processed stations (KZYM 1220) are
    # audible without dynaudnorm doing heavy lifting. Heavily-processed stations
    # (KMOX 1120) may hit the soft clip on talk peaks — acceptable trade.
    OUTPUT_SCALE = np.float32(0.7)

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    stdout = sys.stdout.buffer

    while running:
        sr = sdr.readStream(rx, [raw], BLOCK_COMPLEX, timeoutUs=200_000)
        if sr.ret <= 0:
            continue
        n = sr.ret

        i = raw[0:2 * n:2].astype(np.float32)
        q = raw[1:2 * n:2].astype(np.float32)
        iq = (i + 1j * q).astype(np.complex64) / np.float32(32768.0)
        accum = np.concatenate((accum, iq)) if len(accum) else iq

        while len(accum) >= BLOCK_COMPLEX:
            chunk = accum[:BLOCK_COMPLEX]
            accum = accum[BLOCK_COMPLEX:]

            # Frequency-shift to baseband
            t = np.arange(BLOCK_COMPLEX, dtype=np.float64) + sample_n
            nco = np.exp(1j * nco_omega * t).astype(np.complex64)
            mixed = chunk * nco
            sample_n += BLOCK_COMPLEX

            # Two-stage decimating filter
            y1, hist1 = conv_decim(mixed, TAPS1, DECIM1, hist1)
            y2, hist2 = conv_decim(y1, TAPS2, DECIM2, hist2)

            N = len(y2)

            # PLL lock phase: collect ~0.5 s, FFT-search for carrier offset, lock NCO
            if not fft_locked:
                fft_acc.append(y2)
                total = sum(len(b) for b in fft_acc)
                if total >= fft_target_samples:
                    buf = np.concatenate(fft_acc)
                    n_fft = 1 << (buf.size.bit_length() - 1)  # largest pow2 ≤ buf
                    buf = buf[:n_fft]
                    spec = np.fft.fft(buf * np.hanning(n_fft).astype(np.complex64))
                    freqs = np.fft.fftfreq(n_fft, sample_period)
                    mask = np.abs(freqs) < 200  # search ±200 Hz around DC
                    idx = np.where(mask)[0]
                    peak = idx[np.argmax(np.abs(spec[idx]))]
                    nco_freq_hz = float(freqs[peak])
                    fft_locked = True
                    fft_acc = []
                    sys.stderr.write(
                        f"am_stream: PLL locked at carrier offset {nco_freq_hz:+.2f} Hz\n"
                    )
                    sys.stderr.flush()
                    # Fall through to demod this block
                else:
                    # Pre-lock: output envelope so the user hears something during the
                    # ~0.5 s lock window (avoids a startup silence).
                    env = np.abs(y2).astype(np.float32)
                    env_mean = np.float32(env.mean())
                    sig_amp = (1 - sig_alpha) * sig_amp + sig_alpha * env_mean
                    audio = env - env_mean
                    scaled = np.clip(audio / max(sig_amp, SIG_FLOOR) * OUTPUT_SCALE, -1.0, 1.0)
                    pcm = (scaled * 30_000).astype(np.int16)
                    stdout.write(pcm.tobytes())
                    stdout.flush()
                    continue

            # PLL active: mix down by NCO so the carrier lands at true DC
            ph = nco_phase + 2 * np.pi * nco_freq_hz * sample_period * np.arange(N, dtype=np.float64)
            local_nco = np.exp(1j * ph).astype(np.complex64)
            mixed = (y2 * np.conj(local_nco)).astype(np.complex64)
            nco_phase = (nco_phase + 2 * np.pi * nco_freq_hz * sample_period * N) % (2 * np.pi)

            # Demodulate: real part of de-rotated signal is C + m(t); subtract DC
            real_part = mixed.real.astype(np.float32)
            audio_dc = np.float32(0.98) * audio_dc + np.float32(0.02) * np.float32(real_part.mean())
            audio = real_part - audio_dc

            # Amplitude tracking via envelope mean (robust to lock errors)
            env_block_mean = np.float32(np.mean(np.abs(y2)))
            sig_amp = (1 - sig_alpha) * sig_amp + sig_alpha * env_block_mean
            scaled = np.clip(audio / max(sig_amp, SIG_FLOOR) * OUTPUT_SCALE, -1.0, 1.0)

            pcm = (scaled * 30_000).astype(np.int16)
            stdout.write(pcm.tobytes())
            stdout.flush()

    try:
        sdr.deactivateStream(rx)
        sdr.closeStream(rx)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
