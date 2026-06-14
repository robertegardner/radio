#!/usr/bin/env python3
"""wbfm_stream.py — WBFM+RDS demodulator that reads IQ via SoapySDR directly.

Replaces rx_fm on the REMOTE (radio-compute) path. rx_fm under SoapyRemote
corrupts the stream: it mishandles the small MTU-sized partial reads the remote
server delivers (~1006 samples/datagram), breaking FM demod continuity. The
audible result is garbled FM with dead RDS — misdiagnosed in the 2026-06-13 V2
rollback as "UDP packet loss." It is NOT a transport fault: the identical IQ,
forced onto lossless TCP (remote:prot=tcp), demods cleanly with rich RDS through
a SoapySDR client that accumulates reads properly (proven: 336 RDS groups from a
backpressure-free capture). This is the same reason am_stream.py already replaced
rx_fm for AM.

Pipeline drop-in: emits s16le mono MPX at 250 kHz on stdout, exactly like
`rx_fm ... -M fm -A std -s 250000 - `, so stream.sh keeps the same downstream
tee -> redsea (RDS) + ffmpeg (de-emphasis -> mp3 -> Icecast).

DSP: quadrature FM discriminator at 2 Msps -> anti-alias lowpass (keeps the 57k
RDS subcarrier) -> decimate by 8 -> 250 kHz MPX. Sample-to-sample phase
continuity is maintained across reads (the thing rx_fm gets wrong remotely).

Device + tune come from the stream env files (same contract as stream.sh):
  /etc/sdr-streams/active.env       FREQ, GAIN
  /etc/radio-compute/source-dx-r2.env  SOAPY_ARGS (driver=remote,remote=...,remote:driver=sdrplay)
The SoapyRemote IQ transport is forced to TCP via the setupStream args.

numpy-only (no scipy), to match am_stream.py's runtime.
"""
import os
import signal
import sys
from pathlib import Path

import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_CS16, SOAPY_SDR_RX

HW_RATE = 2_000_000
DECIM = 8
OUT_RATE = HW_RATE // DECIM          # 250_000
CUTOFF = 120_000.0                   # anti-alias; keeps the 57 kHz RDS subcarrier
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


def lowpass_taps(num_taps: int, cutoff: float, fs: float) -> np.ndarray:
    n = np.arange(num_taps) - (num_taps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.hamming(num_taps)
    return (h / h.sum()).astype(np.float32)


def main() -> int:
    active = read_env("/etc/sdr-streams/active.env")
    src = read_env("/etc/radio-compute/source-dx-r2.env")

    freq = parse_freq(active.get("FREQ", "100.7M"))
    gain = float(active.get("GAIN", 30))
    soapy_args = src.get("SOAPY_ARGS", "driver=remote,remote=radio.srvr:55001,remote:driver=sdrplay")
    # Force lossless TCP for the IQ stream (same intent as RX_STREAM_ARGS in stream.sh).
    prot = os.environ.get("RX_STREAM_ARGS", "remote:prot=tcp")
    stream_args = dict(kv.split("=", 1) for kv in prot.split(",") if "=" in kv)

    sys.stderr.write(
        f"wbfm_stream: args={soapy_args!r} freq={freq/1e6:.3f}MHz gain={gain} "
        f"ant={ANTENNA!r} rate={HW_RATE} -> MPX {OUT_RATE} stream_args={stream_args}\n"
    )
    sys.stderr.flush()

    dev = SoapySDR.Device(soapy_args)
    dev.setSampleRate(SOAPY_SDR_RX, 0, HW_RATE)
    try:
        dev.setAntenna(SOAPY_SDR_RX, 0, ANTENNA)
    except Exception as e:
        sys.stderr.write(f"wbfm_stream: setAntenna({ANTENNA!r}) failed: {e}\n")
    # Leave hardware AGC at its default (FM broadcast is fine on AGC; this matches
    # the V1 rx_fm behavior that decodes RDS richly). Set the requested gain anyway.
    dev.setGain(SOAPY_SDR_RX, 0, gain)
    dev.setFrequency(SOAPY_SDR_RX, 0, freq)

    sys.stderr.write(
        f"wbfm_stream: device rate {dev.getSampleRate(SOAPY_SDR_RX, 0):.0f} "
        f"freq {dev.getFrequency(SOAPY_SDR_RX, 0):.0f} ant {dev.getAntenna(SOAPY_SDR_RX, 0)!r}\n"
    )
    sys.stderr.flush()

    st = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16, [0], stream_args)
    dev.activateStream(st)

    taps = lowpass_taps(127, CUTOFF, HW_RATE)
    chunk = 1 << 16                                   # 65536 complex samples/read
    raw = np.empty(2 * chunk, np.int16)              # CS16 interleaved I,Q
    prev = np.complex64(0)                            # discriminator continuity
    fir_hist = np.zeros(len(taps) - 1, dtype=np.float32)
    phase_carry = 0                                   # decimation phase across reads
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

        # Quadrature FM discriminator (phase-continuous across reads).
        x = np.concatenate(([prev], iq))
        disc = np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)
        prev = iq[-1]

        # Anti-alias lowpass with carried history, then phase-aligned decimate.
        ext = np.concatenate((fir_hist, disc))
        filt = np.convolve(ext, taps, mode="valid").astype(np.float32)
        fir_hist = disc[-(len(taps) - 1):].copy()

        idx = np.arange(phase_carry, len(filt), DECIM)
        mpx = filt[idx]
        phase_carry = (phase_carry - len(filt)) % DECIM

        # ±pi -> full scale; WBFM deviation stays well clear of clip.
        pcm = np.clip(mpx * np.float32(32767.0 / np.pi), -32767, 32767).astype(np.int16)
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
