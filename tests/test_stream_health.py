"""Tests for stream_health journal/icecast parsing (pure functions only —
the subprocess/HTTP collectors are exercised live on the rack)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "files" / "opt" / "sdr-tuner"))

import stream_health as sh


NOW = 1_000_000.0


class SummarizeEvents(unittest.TestCase):
    def test_counts_overflow_bursts_and_chars(self):
        entries = [
            (NOW - 30, "OOOOOO"),          # 6 O's, minute bucket 0
            (NOW - 90, "OOO"),             # 3 O's, bucket 1
            (NOW - 30, "stereo_decode: pilot_rms=0.0048 blend=1.00"),
        ]
        r = sh.summarize_events(entries, now=NOW)
        self.assertEqual(r["overflow_bursts"], 2)
        self.assertEqual(r["overflow_samples"], 9)
        self.assertEqual(r["overflow_last_ts"], NOW - 30)
        self.assertEqual(r["per_min"][0], 6)
        self.assertEqual(r["per_min"][1], 3)
        self.assertEqual(sum(r["per_min"]), 9)

    def test_ignores_non_overflow_messages(self):
        entries = [(NOW - 10, "wbfm_stream: device rate 2000000"),
                   (NOW - 20, "[INFO] Using format CS16.")]
        r = sh.summarize_events(entries, now=NOW)
        self.assertEqual(r["overflow_bursts"], 0)
        self.assertIsNone(r["overflow_last_ts"])

    def test_detects_service_restarts(self):
        entries = [
            (NOW - 120, "Started sdr-fm@active.service - SDR FM stream active (rack, remote dx-R2)."),
            (NOW - 40, "OO"),
        ]
        r = sh.summarize_events(entries, now=NOW)
        self.assertEqual(r["restarts"], [NOW - 120])

    def test_burst_ts_capped(self):
        entries = [(NOW - i, "O") for i in range(300)]
        r = sh.summarize_events(entries, now=NOW)
        self.assertLessEqual(len(r["burst_ts"]), sh.BURST_TS_CAP)

    def test_out_of_window_bucket_dropped_but_counted(self):
        # 11 min old: journalctl shouldn't return it, but be robust
        entries = [(NOW - 660, "OOOO")]
        r = sh.summarize_events(entries, now=NOW, window_sec=600)
        self.assertEqual(r["overflow_bursts"], 0)


class ExtractMount(unittest.TestCase):
    STATUS = {"icestats": {"source": [
        {"listenurl": "http://icecast.rg2.io:8000/ems.mp3", "listeners": 2,
         "stream_start_iso8601": "2026-08-13T01:51:05+0000"},
        {"listenurl": "http://icecast.rg2.io:8000/fm.mp3", "listeners": 3,
         "listener_peak": 5, "stream_start_iso8601": "2026-08-24T18:12:59+0000"},
    ]}}

    def test_finds_mount_and_uptime(self):
        # 2026-08-24T18:13:59Z == 60 s after stream_start
        from datetime import datetime, timezone
        now = datetime(2026, 8, 24, 18, 13, 59, tzinfo=timezone.utc).timestamp()
        r = sh.extract_mount(self.STATUS, "/fm.mp3", now=now)
        self.assertTrue(r["ok"])
        self.assertEqual(r["listeners"], 3)
        self.assertEqual(r["listener_peak"], 5)
        self.assertAlmostEqual(r["mount_uptime_sec"], 60.0, delta=1.0)

    def test_single_source_dict(self):
        from datetime import datetime, timezone
        status = {"icestats": {"source":
                  {"listenurl": "http://x/fm.mp3", "listeners": 1,
                   "stream_start_iso8601": "2026-08-24T18:12:59+0000"}}}
        now = datetime(2026, 8, 24, 18, 13, 59, tzinfo=timezone.utc).timestamp()
        r = sh.extract_mount(status, "/fm.mp3", now=now)
        self.assertTrue(r["ok"])
        self.assertEqual(r["listeners"], 1)

    def test_missing_mount(self):
        r = sh.extract_mount({"icestats": {"source": []}}, "/fm.mp3", now=NOW)
        self.assertFalse(r["ok"])


class ParseJournalJson(unittest.TestCase):
    def test_parses_lines(self):
        lines = [
            '{"__REALTIME_TIMESTAMP": "1756060000000000", "MESSAGE": "OOOO"}',
            'not json at all',
            '{"__REALTIME_TIMESTAMP": "1756060012000000", "MESSAGE": ["b","i","n"]}',
            '{"MESSAGE": "no ts"}',
        ]
        out = sh.parse_journal_json(lines)
        self.assertEqual(out, [(1756060000.0, "OOOO")])


if __name__ == "__main__":
    unittest.main()
