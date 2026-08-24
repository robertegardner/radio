"""wbfm_stream DSP equivalence tests: the FFT decimating FIR and the
astype+view sample conversion must match the original (naive) definitions
bit-near-exactly, including phase continuity across ragged read sizes like
SoapyRemote's ~1006-sample datagrams."""
import sys
import unittest
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "files" / "opt" / "sdr-tuner"))

import wbfm_stream as wb


class NaiveDecimatingFIR:
    """Reference: the original matmul formulation (windows at phase, phase+D, ...)."""

    def __init__(self, taps, decim, dtype):
        self.taps = taps.astype(np.float32)
        self.N = len(taps)
        self.D = decim
        self.dtype = dtype
        self.hist = np.zeros(self.N - 1, dtype=dtype)
        self.phase = 0

    def __call__(self, x):
        ext = np.concatenate((self.hist, x))
        nwin = ext.shape[0] - self.N + 1
        if nwin <= 0:
            self.hist = ext[-(self.N - 1):].copy()
            return np.empty(0, dtype=self.dtype)
        win = sliding_window_view(ext, self.N)
        out = (win[self.phase::self.D] @ self.taps).astype(self.dtype)
        self.phase = (self.phase - nwin) % self.D
        self.hist = ext[-(self.N - 1):].copy()
        return out


RAGGED = [1006, 977, 65536, 503, 12, 1006, 2012, 198, 40000]


class FirEquivalence(unittest.TestCase):
    def _run(self, taps, decim, dtype, scale):
        rng = np.random.default_rng(7)
        ref = NaiveDecimatingFIR(taps, decim, dtype)
        new = wb.DecimatingFIR(taps, decim, dtype)
        for n in RAGGED:
            if dtype == np.complex64:
                x = (rng.standard_normal(2 * n).astype(np.float32).view(np.complex64)) * scale
            else:
                x = rng.standard_normal(n).astype(np.float32) * scale
            a, b = ref(x), new(x)
            self.assertEqual(len(a), len(b), f"length mismatch at chunk {n}")
            if len(a):
                # float32 FFT vs time-domain: agree to ~1e-6 relative
                tol = scale * 1e-4
                self.assertLess(float(np.abs(a - b).max()), tol, f"chunk {n}")

    def test_stage1_complex_decim4(self):
        taps = wb.lowpass_taps(wb.CHAN_TAPS, wb.CHAN_CUTOFF, wb.HW_RATE, wb.CHAN_BETA)
        self._run(taps, wb.DECIM1, np.complex64, 30000.0)

    def test_stage3_real_decim2(self):
        taps = wb.lowpass_taps(wb.MPX_TAPS, wb.MPX_CUTOFF, wb.IF_RATE)
        self._run(taps, wb.DECIM2, np.float32, 3.0)

    def test_tiny_chunks_no_output_then_catchup(self):
        taps = wb.lowpass_taps(199, 150e3, 2e6, 8.5)
        ref = NaiveDecimatingFIR(taps, 4, np.complex64)
        new = wb.DecimatingFIR(taps, 4, np.complex64)
        rng = np.random.default_rng(3)
        for n in [10, 20, 50, 100, 400, 5000]:
            x = (rng.standard_normal(2 * n).astype(np.float32).view(np.complex64))
            a, b = ref(x), new(x)
            self.assertEqual(len(a), len(b))
            if len(a):
                self.assertLess(float(np.abs(a - b).max()), 1e-4)


class SampleConversion(unittest.TestCase):
    def test_view_matches_original_times_32768(self):
        rng = np.random.default_rng(11)
        raw = rng.integers(-32768, 32768, 2048, dtype=np.int16)
        n = 1024
        # original formulation
        i = raw[0:2 * n:2].astype(np.float32)
        q = raw[1:2 * n:2].astype(np.float32)
        iq_old = ((i + 1j * q) / np.float32(32768.0)).astype(np.complex64)
        iq_new = wb.cs16_to_complex(raw, n)
        np.testing.assert_allclose(iq_new, iq_old * np.float32(32768.0), rtol=0, atol=0)


class DiscriminatorScaleInvariance(unittest.TestCase):
    def test_angle_invariant_to_input_scale(self):
        rng = np.random.default_rng(5)
        x = (rng.standard_normal(2000).astype(np.float32).view(np.complex64))
        a1 = np.angle(x[1:] * np.conj(x[:-1]))
        xs = x * np.float32(32768.0)
        a2 = np.angle(xs[1:] * np.conj(xs[:-1]))
        np.testing.assert_allclose(a1, a2, atol=1e-5)


if __name__ == "__main__":
    unittest.main()
