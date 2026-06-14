#!/usr/bin/env python3
"""wbfm_stream.py — WBFM+RDS demodulator that reads IQ via SoapySDR directly.

Replaces rx_fm on the REMOTE (radio-compute) path. rx_fm under SoapyRemote
corrupts the stream: it mishandles the small MTU-sized partial reads the remote
server delivers (~1006 samples/datagram), breaking FM demod continuity. The
audible result is garbled FM with dead RDS — misdiagnosed in the 2026-06-13 V2
rollback as "UDP packet loss." It is NOT a transport fault: the identical IQ,
forced onto lossless TCP (remote:prot=tcp), demods cleanly with rich RDS through
a SoapySDR client that accumulates reads properly. Same reason am_stream.py
already replaced rx_fm for AM.

Pipeline drop-in: emits s16le mono MPX at 250 kHz on stdout, exactly like
`rx_fm ... -M fm -A std -s 250000 - `, so stream.sh keeps the same downstream
tee -> redsea (RDS) + stereo_decode.py + ffmpeg.

DSP — CHANNEL-SELECT FIRST, THEN DISCRIMINATE (fixed 2026-06-14), TWO-STAGE:
    readStream (2.0 MHz complex IQ, tuned station at DC in Zero-IF)
      -> Stage 1: complex decimating low-pass FIR, cutoff ~160 kHz, 2.0M -> 500k
         channel IQ  (passes the WHOLE desired FM signal — Carson BW ~±128 kHz —
         while rejecting neighbours ≥±400 kHz, e.g. the +600 kHz ghosts)
      -> Stage 2: quadrature FM discriminator on the 500k single-channel IQ
      -> Stage 3: real decimating low-pass FIR, cutoff 100 kHz, 500k -> 250k MPX
         (keeps the 57 kHz RDS subcarrier; this is the MPX bandlimit + final /2)

Why two stages: the original build ran the discriminator on the FULL 2.0 MHz
window with NO channel filter and capture-effected onto the strongest carrier
anywhere in ~±0.9 MHz (KGMO 100.7 was audible 99.8–101.6). A single-stage
channel filter straight to 250k (Nyquist 125 kHz) is too narrow to pass the full
FM signal — clipping the upper sidebands corrupts the top of the MPX, i.e. the
57 kHz RDS, and RDS won't lock. So we channel-select at a 500 kHz intermediate
(room for the full FM signal AND a clean anti-alias skirt), discriminate there,
then decimate the demodulated MPX to 250 kHz — exactly what the multistation
csdr path does (shift -> 2-stage decimate -> fmdemod). The wanted signal is at DC
so the channel filter is centred at DC; there is NO DC notch.

Phase continuity is maintained across reads in BOTH FIRs (overlap-save history)
and the discriminator (carried last sample) — the thing rx_fm gets wrong remotely.

Device + tune come from the stream env files (same contract as stream.sh):
  /etc/sdr-streams/active.env          FREQ, GAIN
  /etc/radio-compute/source-dx-r2.env  SOAPY_ARGS (driver=remote,...,remote:driver=sdrplay)
The SoapyRemote IQ transport is forced to TCP via the setupStream args.

numpy-only (no scipy), to match am_stream.py / stereo_decode.py runtime.
"""
import os
import signal
import sys
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
import SoapySDR
from SoapySDR import SOAPY_SDR_CS16, SOAPY_SDR_RX

HW_RATE = 2_000_000                  # dx-R2 device rate — DO NOT change
DECIM1 = 4                           # stage 1: 2.0M -> 500k intermediate (full FM)
IF_RATE = HW_RATE // DECIM1          # 500_000 — discriminator runs here
DECIM2 = 2                           # stage 3: 500k -> 250k MPX
OUT_RATE = IF_RATE // DECIM2         # 250_000 — the rate stereo_decode/redsea/ffmpeg expect
CHAN_CUTOFF = 150_000.0            # stage-1 channel select: pass the whole FM channel
                                     # (~±128 kHz Carson) flat, reject neighbours hard
CHAN_TAPS = 199                      # Kaiser β=8.5 + 150 kHz: 128k≈0 dB (full FM/RDS),
CHAN_BETA = 8.5                      # ±200 kHz first-adjacent −90 dB, ±600 kHz −119 dB.
                                     # (Hamming 127@160k left ±200 kHz only −64 dB, which
                                     # bled a first-adjacent onto strong NPR 90.9 in the
                                     # packed 88–92 band.)
MPX_CUTOFF = 100_000.0            # stage-3 MPX bandlimit; keeps the 57 kHz RDS subcarrier
MPX_TAPS = 127
ANTENNA = "Antenna A"                # FM (Shakespeare 5120 + bandpass)


def read_env(path: str) -> dict:
    env: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def parse_freq(s: str) -> float:
    s = s.strip()
    mult = 1.0
    if s and s[-1] in "kK":
        mult, s = 1_000.0, s[:-1]
    elif s and s[-1] in "mM":
        mult, s = 1_000_000.0, s[:-1]
    return float(s) * mult


def lowpass_taps(num_taps: int, cutoff: float, fs: float, beta: float = 0.0) -> np.ndarray:
    """Windowed-sinc lowpass, unity DC gain, real taps. Hamming by default; pass a
    Kaiser beta (>0) for a deeper, tunable stopband (channel-select needs it)."""
    n = np.arange(num_taps) - (num_taps - 1) / 2
    win = np.kaiser(num_taps, beta) if beta > 0 else np.hamming(num_taps)
    h = np.sinc(2 * cutoff / fs * n) * win
    return (h / h.sum()).astype(np.float32)


class DecimatingFIR:
    """Streaming decimating FIR (real taps; complex or real samples). Phase-
    continuous across reads and computes ONLY the retained (decimated) outputs —
    the strided slice of a sliding_window_view never materialises the discarded
    full-rate samples."""

    def __init__(self, taps: np.ndarray, decim: int, dtype):
        self.taps = taps.astype(np.float32)
        self.N = len(taps)
        self.D = decim
        self.dtype = dtype
        self.hist = np.zeros(self.N - 1, dtype=dtype)
        self.phase = 0  # full-rate index of this block's first kept output

    def __call__(self, x: np.ndarray) -> np.ndarray:
        ext = np.concatenate((self.hist, x))
        nwin = ext.shape[0] - self.N + 1
        if nwin <= 0:
            self.hist = ext[-(self.N - 1):].copy()
            return np.empty(0, dtype=self.dtype)
        win = sliding_window_view(ext, self.N)       # (nwin, N) view, no copy
        out = (win[self.phase::self.D] @ self.taps).astype(self.dtype)
        self.phase = (self.phase - nwin) % self.D    # carry decimation phase
        self.hist = ext[-(self.N - 1):].copy()
        return out


def main() -> int:
    active = read_env("/etc/sdr-streams/active.env")
    src = read_env("/etc/radio-compute/source-dx-r2.env")

    freq = parse_freq(active.get("FREQ", "100.7M"))
    gain = float(active.get("GAIN", 30))
    antenna = active.get("ANTENNA") or ANTENNA  # FM: "Antenna A" (Shakespeare) or
                                                # "Antenna B" (dipole + Sawbird LNA)
    soapy_args = src.get("SOAPY_ARGS", "driver=remote,remote=radio.srvr:55001,remote:driver=sdrplay")
    prot = os.environ.get("RX_STREAM_ARGS", "remote:prot=tcp")
    stream_args = dict(kv.split("=", 1) for kv in prot.split(",") if "=" in kv)

    sys.stderr.write(
        f"wbfm_stream: args={soapy_args!r} freq={freq/1e6:.3f}MHz gain={gain} "
        f"ant={antenna!r} rate={HW_RATE} -> IF {IF_RATE} (chan ±{CHAN_CUTOFF:.0f}) "
        f"-> MPX {OUT_RATE} stream_args={stream_args}\n"
    )
    sys.stderr.flush()

    dev = SoapySDR.Device(soapy_args)
    dev.setSampleRate(SOAPY_SDR_RX, 0, HW_RATE)
    try:
        dev.setAntenna(SOAPY_SDR_RX, 0, antenna)
    except Exception as e:
        sys.stderr.write(f"wbfm_stream: setAntenna({antenna!r}) failed: {e}\n")
    dev.setGain(SOAPY_SDR_RX, 0, gain)
    dev.setFrequency(SOAPY_SDR_RX, 0, freq)

    sys.stderr.write(
        f"wbfm_stream: device rate {dev.getSampleRate(SOAPY_SDR_RX, 0):.0f} "
        f"freq {dev.getFrequency(SOAPY_SDR_RX, 0):.0f} ant {dev.getAntenna(SOAPY_SDR_RX, 0)!r}\n"
    )
    sys.stderr.flush()

    st = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16, [0], stream_args)
    dev.activateStream(st)

    chan_fir = DecimatingFIR(lowpass_taps(CHAN_TAPS, CHAN_CUTOFF, HW_RATE, CHAN_BETA), DECIM1, np.complex64)
    mpx_fir = DecimatingFIR(lowpass_taps(MPX_TAPS, MPX_CUTOFF, IF_RATE), DECIM2, np.float32)
    # Discriminator phase-step is 2*pi*f_inst/IF_RATE, i.e. DECIM1 x larger than the
    # old build's 2*pi*f_inst/HW_RATE. Divide by DECIM1 so the MPX amplitude matches
    # what stereo_decode (--pilot-floor) and redsea were calibrated to (and so the
    # ±pi-bounded phase step lands well clear of the int16 clip).
    disc_scale = np.float32(32767.0 / (np.pi * DECIM1))

    chunk = 1 << 16                                   # 65536 complex samples/read
    raw = np.empty(2 * chunk, np.int16)              # CS16 interleaved I,Q
    prev = np.complex64(0)                            # discriminator continuity (IF domain)
    stdout = sys.stdout.buffer

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        sr = dev.readStream(st, [raw], chunk, timeoutUs=1_000_000)
        n = sr.ret
        if n <= 0:
            continue                                  # overflow/timeout: skip (rare on TCP)

        i = raw[0:2 * n:2].astype(np.float32)
        q = raw[1:2 * n:2].astype(np.float32)
        iq = ((i + 1j * q) / np.float32(32768.0)).astype(np.complex64)

        # Stage 1: channel-select + decimate to the 500k IF (isolate the tuned
        # station at DC; reject neighbours so the discriminator can't capture them).
        chan = chan_fir(iq)
        if chan.size == 0:
            continue

        # Stage 2: quadrature FM discriminator on the single-channel IF IQ.
        x = np.concatenate((np.array([prev], dtype=np.complex64), chan))
        disc = np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)
        prev = chan[-1]

        # Stage 3: MPX bandlimit (keeps 0–100 kHz incl. 57 kHz RDS) + decimate to 250k.
        mpx = mpx_fir(disc)
        if mpx.size == 0:
            continue

        pcm = np.clip(mpx * disc_scale, -32767, 32767).astype(np.int16)
        stdout.write(pcm.tobytes())
        stdout.flush()

    try:
        dev.deactivateStream(st)
        dev.closeStream(st)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
