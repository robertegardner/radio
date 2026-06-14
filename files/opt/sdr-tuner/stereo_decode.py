#!/usr/bin/env python3
"""stereo_decode.py — FM stereo decoder for the multistation IQ path.

Reads the demodulated FM multiplex (MPX / composite) as real float32 at 250 ksps
on stdin — the output of `csdr fmdemod_quadri_cf` in channel_pipeline.sh — and
writes interleaved s16le STEREO at 250 ksps on stdout. ffmpeg downstream does the
75 µs de-emphasis (per channel) and the resample to 48 kHz, exactly as the legacy
mono path already does — so this stage is purely the stereo matrix.

Why pilot-squaring (not a PLL): a per-sample phase-locked loop can't be
vectorized (each sample depends on the last NCO phase), and a 250 ksps Python
sample loop won't hold. Squaring the recovered 19 kHz pilot produces a 38 kHz
tone that is inherently phase-coherent with it (cos²θ = ½(1+cos2θ)), which is all
the L−R subcarrier needs for coherent detection — and it's all FIRs + elementwise
math, which numpy vectorizes cleanly. Strong locals decode with textbook
separation this way; the L−R noise penalty that matters only for weak signals is
handled by the honesty gate below.

The MPX bands (US FM):
    0–15 kHz   L+R (mono sum)
    19 kHz     stereo pilot
    23–53 kHz  L−R, DSB-SC about a 38 kHz suppressed carrier
    57 kHz     RDS
Decode:
    sum  = lpf15( MPX )                         # L+R
    c38  = bpf38( bpf19(MPX)² ), normalized      # coherent 38 kHz carrier
    diff = lpf15( MPX · 2·c38 )                  # L−R baseband
    L = (sum + diff)/2 ,  R = (sum − diff)/2

Stereo honesty gate (invariant 4): the L−R subcarrier sits at 38 kHz where FM SNR
is worst, so a weak station sounds *worse* in forced stereo. We track the pilot
level and continuously blend `diff` toward zero (→ mono) as the pilot weakens, so
a distant affiliate degrades gracefully to mono instead of hissing in stereo.
`--stereo 0` forces mono outright (skip this stage entirely — channel_pipeline.sh
does that). `--pilot-floor` sets where the blend reaches full mono.
"""
import argparse
import json
import os
import sys
import time

import numpy as np

FS = 250_000  # composite rate out of csdr (8 Msps / 32)

# Pilot-lock status for the UI (true 19 kHz pilot detection, not the chosen mode).
# Written ~2x/sec; the tuner API freshness-gates it so a stale file reads as mono.
PILOT_STATUS_PATH = "/run/sdr-streams/pilot.json"


def _write_pilot(rms: float, blend: float) -> None:
    """Publish pilot lock atomically. `locked` = clearly receiving a stereo pilot
    (blend past the half-way point of the honesty gate's mono→stereo ramp)."""
    try:
        tmp = PILOT_STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), "pilot_rms": round(rms, 5),
                       "blend": round(blend, 3), "stereo": bool(blend >= 0.5)}, f)
        os.replace(tmp, PILOT_STATUS_PATH)
    except OSError:
        pass  # status is best-effort; never let it break the audio path


def _lowpass(ntaps: int, cutoff: float, fs: float) -> np.ndarray:
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.hamming(ntaps)
    return (h / h.sum()).astype(np.float32)


def _bandpass(ntaps: int, f_lo: float, f_hi: float, fs: float) -> np.ndarray:
    """Windowed-sinc bandpass, unity gain at band center."""
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = (2 * f_hi / fs) * np.sinc(2 * f_hi / fs * n) - \
        (2 * f_lo / fs) * np.sinc(2 * f_lo / fs * n)
    h *= np.hamming(ntaps)
    fc = 0.5 * (f_lo + f_hi)
    gain = float(np.sum(h * np.cos(2 * np.pi * fc / fs * n)))
    if abs(gain) > 1e-9:
        h = h / gain
    return h.astype(np.float32)


# Pilot: narrow about 19 kHz so squaring isn't polluted by neighbouring bands.
TAPS_PILOT = _bandpass(151, 18_700, 19_300, FS)
# 38 kHz carrier extracted from the squared pilot.
TAPS_C38 = _bandpass(151, 37_000, 39_000, FS)
# 15 kHz audio lowpass on sum and diff (so the int16 PCM carries clean baseband
# audio at full scale, rather than full-MPX peaks ffmpeg would just discard).
TAPS_LP15 = _lowpass(151, 15_000, FS)


class Fir:
    """Streaming FIR (overlap-save): output aligns 1:1 with input, no decimation."""

    def __init__(self, taps: np.ndarray):
        self.taps = taps
        self.hist = np.zeros(len(taps) - 1, dtype=np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        ext = np.concatenate((self.hist, x))
        y = np.convolve(ext, self.taps, mode="valid").astype(np.float32)
        self.hist = x[-(len(self.taps) - 1):].copy()
        return y


class Delay:
    """Streaming integer-sample delay line. output[i] = input[i - d]."""

    def __init__(self, d: int):
        self.d = d
        self.buf = np.zeros(d, dtype=np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.d == 0:
            return x
        ext = np.concatenate((self.buf, x))
        out = ext[: len(x)]
        self.buf = ext[len(x):]
        return out


# The regenerated 38 kHz carrier comes out of bp19 -> square -> bp38, so it is
# group-delayed by the two linear-phase bandpasses relative to the raw MPX it has
# to multiply. If we don't compensate, carrier and L-R subcarrier are phase-
# misaligned and separation collapses (the L-R term comes out scaled by cos(phase
# error)). Delay the MPX feeding BOTH sum and diff by that same group delay so
# everything lines up; the shared 15 kHz lowpass delay then cancels in the matrix.
ALIGN = (len(TAPS_PILOT) - 1) // 2 + (len(TAPS_C38) - 1) // 2


def main() -> int:
    ap = argparse.ArgumentParser(description="FM stereo decoder (MPX → s16le stereo)")
    ap.add_argument("--scale", type=float, default=3.0,
                    help="output gain applied to matrixed L/R before int16. Tune "
                         "via the out_peak line this logs (keep it ~0.5-0.9 on "
                         "loud passages; >1.0 clips and wrecks separation). "
                         "Default 3.0 (validated clean on a synthetic full-"
                         "deviation MPX; real stations vary).")
    ap.add_argument("--pilot-floor", type=float, default=0.003,
                    help="pilot RMS at/below which output is fully mono; the "
                         "blend ramps to full stereo by ~3x this. Default 0.003")
    ap.add_argument("--in-format", choices=("f32", "s16le"), default="f32",
                    help="input MPX sample format. f32 = the csdr/multistation path "
                         "(default). s16le = the rack single-station path "
                         "(wbfm_stream.py emits s16le MPX; scaled by /32768 to ~unit "
                         "amplitude, matching csdr's float output).")
    ap.add_argument("--out-format", choices=("s16le", "f32le"), default="s16le",
                    help="output STEREO sample format. s16le (default) hard-clips at "
                         "+-1.0 before quantizing — fine when ffmpeg de-emphasis runs "
                         "AFTER (mux path). f32le emits unclipped float so the loud "
                         "PRE-emphasis peaks survive into ffmpeg's float de-emphasis + "
                         "limiter (rack path: prevents int16 clipping on loud peaks).")
    ap.add_argument("--carrier-alpha", type=float, default=0.9,
                    help="EMA factor for the regenerated 38 kHz carrier amplitude, "
                         "smoothed ACROSS blocks. The round-1 per-block RMS normalize "
                         "stepped the carrier scale at every block boundary on a noisy "
                         "pilot → clicks. Smoothing removes the steps. Default 0.9.")
    ap.add_argument("--carrier-clamp", type=float, default=1.3,
                    help="clamp |normalized carrier| to this. A clean carrier is ~±1.0; "
                         "a noisy live pilot drives instantaneous crest to 3–4× → those "
                         "transients multiply into L−R as clicks. Clamping kills them "
                         "with no effect on clean signal (offline A/B: 12× fewer clicks "
                         "on a buried-pilot stress case, identical on a clean dump). "
                         "Default 1.3.")
    args = ap.parse_args()

    fir_pilot = Fir(TAPS_PILOT)
    fir_c38 = Fir(TAPS_C38)
    fir_sum = Fir(TAPS_LP15)
    fir_diff = Fir(TAPS_LP15)
    delay_mpx = Delay(ALIGN)  # align raw MPX to the regenerated 38 kHz carrier

    scale = np.float32(args.scale)
    floor = float(args.pilot_floor)
    full = max(floor * 3.0, floor + 1e-6)  # pilot level for full stereo
    carrier_alpha = np.float32(args.carrier_alpha)
    carrier_clamp = np.float32(args.carrier_clamp)

    # Block of 8192 float32 (~33 ms at 250k) — enough for cheap FIRs, small
    # enough for low latency. stdin may hand us short reads; loop until we have
    # a whole block or EOF.
    BLOCK = 8192
    bps = 2 if args.in_format == "s16le" else 4   # bytes per input MPX sample
    nbytes = BLOCK * bps
    out_f32 = args.out_format == "f32le"
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    pilot_lvl = np.float32(0.0)
    c38_amp_st = np.float32(0.0)  # carrier amplitude, smoothed across blocks
    prev_blend = 0.0              # last block's blend, for a continuous ramp
    peak_out = 0.0
    clamp_hits = 0               # samples the carrier clamp caught (telemetry)
    clamp_total = 0
    blocks = 0

    while True:
        raw = stdin.read(nbytes)
        if not raw:
            break
        if len(raw) < nbytes:
            # final partial block — trim to a whole number of input samples
            usable = (len(raw) // bps) * bps
            if usable == 0:
                break
            raw = raw[:usable]
        if bps == 2:
            mpx = np.frombuffer(raw, dtype="<i2").astype(np.float32) / np.float32(32768.0)
        else:
            mpx = np.frombuffer(raw, dtype=np.float32)

        pilot = fir_pilot(mpx)
        # coherent 38 kHz carrier from the squared pilot, normalized to ~unit amp.
        c38 = fir_c38(pilot * pilot)
        blk_amp = np.float32(np.sqrt(np.mean(c38 * c38)) * np.sqrt(2.0))
        # Smooth the carrier amplitude ACROSS blocks (EMA) instead of normalizing
        # per-block: a per-block scale steps the carrier at every block boundary on
        # a noisy pilot, and that step is a click. Prime on the first block.
        if c38_amp_st <= 0:
            c38_amp_st = blk_amp if blk_amp > 1e-6 else np.float32(1e-6)
        else:
            c38_amp_st = carrier_alpha * c38_amp_st + (np.float32(1.0) - carrier_alpha) * blk_amp
        amp = c38_amp_st if c38_amp_st > 1e-6 else np.float32(1e-6)
        c38n = c38 / amp
        # Clamp the instantaneous crest: a clean carrier is ~±1, a noisy pilot spikes
        # to 3–4× and those transients become clicks in L−R. Clamping bounds them
        # without touching the clean case.
        clamp_total += c38n.size
        clamp_hits += int(np.count_nonzero(np.abs(c38n) > carrier_clamp))
        np.clip(c38n, -carrier_clamp, carrier_clamp, out=c38n)

        # Delay the MPX to time-align with the carrier regenerated above, then
        # take both the sum and the (coherently-detected) diff from it.
        mpx_d = delay_mpx(mpx)
        sum_lr = fir_sum(mpx_d)               # L+R
        diff_lr = fir_diff(mpx_d * c38n * 2.0)  # L−R (baseband after lpf)

        # Honesty gate: smooth the pilot level, blend diff → 0 as it weakens.
        blk_pilot = np.float32(np.sqrt(np.mean(pilot * pilot)))
        pilot_lvl = np.float32(0.9) * pilot_lvl + np.float32(0.1) * blk_pilot
        blend = (float(pilot_lvl) - floor) / (full - floor)
        blend = float(min(1.0, max(0.0, blend)))
        # Ramp the gain across the block from the previous block's blend to this
        # one's — a per-block step in the diff gain is itself an audible click.
        ramp = np.linspace(prev_blend, blend, diff_lr.size, dtype=np.float32)
        diff_lr *= ramp
        prev_blend = blend

        left = (sum_lr + diff_lr) * np.float32(0.5) * scale
        right = (sum_lr - diff_lr) * np.float32(0.5) * scale

        inter = np.empty(left.size * 2, dtype=np.float32)
        inter[0::2] = left
        inter[1::2] = right

        # Running true peak across ALL blocks (not just the sampled one) so the
        # telemetry can't under-report clipping risk.
        peak_out = max(peak_out, float(np.abs(inter).max()))

        if out_f32:
            # Unclipped float — loud pre-emphasis peaks survive into ffmpeg's float
            # de-emphasis + limiter instead of being hard-clipped here.
            stdout.write(inter.astype("<f4").tobytes())
        else:
            np.clip(inter, -1.0, 1.0, out=inter)
            stdout.write((inter * 32767.0).astype("<i2").tobytes())
        stdout.flush()

        blocks += 1
        if blocks % 15 == 0:   # ~0.5 s: publish pilot lock for the UI LED/meter
            _write_pilot(float(pilot_lvl), blend)
        if blocks % 300 == 0:  # ~10 s: level telemetry for tuning the gate/scale
            clamp_frac = (clamp_hits / clamp_total) if clamp_total else 0.0
            sys.stderr.write(
                f"stereo_decode: pilot_rms={float(pilot_lvl):.4f} blend={blend:.2f} "
                f"carrier_amp={float(c38_amp_st):.5f} clamp={clamp_frac*100:.2f}% "
                f"peak_out_max={peak_out:.3f} (scale={float(scale):.1f}, "
                f"clamp={float(carrier_clamp):.1f}, {'f32' if out_f32 else 's16'})\n"
            )
            sys.stderr.flush()
            peak_out = 0.0  # reset window
            clamp_hits = clamp_total = 0  # reset window

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
    finally:
        # Drop the status on exit so a stop/mono-switch reads as no-pilot at once
        # (the API also freshness-gates the file as a backstop against crashes).
        try:
            os.unlink(PILOT_STATUS_PATH)
        except OSError:
            pass
