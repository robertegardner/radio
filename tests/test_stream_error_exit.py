"""Every SoapyRemote read site in the long-running stream clients must bail on
STREAM_ERROR (-2). Calling readStream again after -2 spins forever inside
SoapyRemote's C++ client (platform 2026-10-02 repro: P25 silent ~2 days), so a
read loop that just `continue`s on ret<=0 turns a Pi source reset into a wedge.

Run: python3 -m unittest discover -s tests
"""
import re
import sys
import types
import unittest
from pathlib import Path

SDR = Path(__file__).resolve().parent.parent / "files" / "opt" / "sdr-tuner"
CLIENTS = ["wbfm_stream.py", "am_stream.py"]
READ = re.compile(r"^([ \t]+)sr = \w+\.readStream\(.*\)$")


class StreamErrorExitTest(unittest.TestCase):
    def test_every_read_site_checks_stream_error(self):
        for name in CLIENTS:
            lines = (SDR / name).read_text().splitlines()
            sites = [i for i, l in enumerate(lines) if READ.match(l)]
            self.assertTrue(sites, f"{name}: no readStream sites found")
            for i in sites:
                ind = READ.match(lines[i]).group(1)
                self.assertEqual(
                    lines[i + 1], f"{ind}if sr.ret == STREAM_ERROR:",
                    f"{name}:{i + 2}: readStream not followed by the STREAM_ERROR check")

    def test_stream_dead_exits_nonzero(self):
        # Import wbfm_stream with SoapySDR stubbed (no bindings on dev boxes).
        soapy = types.ModuleType("SoapySDR")
        soapy.SOAPY_SDR_CS16, soapy.SOAPY_SDR_RX = "CS16", 0
        sys.modules.setdefault("SoapySDR", soapy)
        sys.path.insert(0, str(SDR))
        try:
            import wbfm_stream
        except ImportError as exc:  # numpy/scipy missing on this box
            self.skipTest(f"wbfm_stream import needs {exc.name}")
        self.assertEqual(wbfm_stream.STREAM_ERROR, -2)
        with self.assertRaises(SystemExit) as cm:
            wbfm_stream._stream_dead("wbfm_stream")
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
