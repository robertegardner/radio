"""atc-rec holds the discone on ATC for the recording window (hold_until) and
returns it to P25 — the discone default — afterwards (platform 2026-10-02)."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "files" / "opt" / "sdr-tuner" / "atc-rec-tick.py"


def load():
    spec = importlib.util.spec_from_file_location("atc_rec_tick", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class AtcRecHoldTest(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.bodies = []

        class R:
            def read(_self):
                return b"{}"

        def fake_urlopen(req, timeout=None):
            self.bodies.append(json.loads(req.data))
            return R()
        self.patch = mock.patch.object(self.m.urllib.request, "urlopen", fake_urlopen)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_r2_atc_sends_hold_until(self):
        self.m.r2("atc", 125.525, hold_until=1759400000)
        self.assertEqual(self.bodies[-1], {"mode": "atc", "freq": "125.525M", "audio_mode": "am",
                                           "hold_until": 1759400000})

    def test_r2_without_hold_omits_it(self):
        self.m.r2("p25")
        self.assertEqual(self.bodies[-1], {"mode": "p25"})

    def test_source_returns_to_p25_not_noaa(self):
        src = SRC.read_text()
        self.assertNotIn('r2("noaa")', src)
        self.assertIn('r2("p25")', src)
        self.assertIn('hold_until=want["end"]', src)


if __name__ == "__main__":
    unittest.main()
