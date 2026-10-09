"""rds_watcher.py publishes an `rds_seen` heartbeat for platform fm-watch.

fm-watch (platform modules/radio-compute/fm_watch.sh) treats a frequency that
has carried RDS but has decoded none for minutes as a NOISE stream (the
2026-10-05 degraded-source incident) and bounces the source. now_playing.json
is otherwise only rewritten on content CHANGE, so a steady station would look
stale; `rds_seen` is refreshed (throttled) on every decoded group.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WATCHER = Path(__file__).resolve().parent.parent / "files" / "opt" / "sdr-tuner" / "rds_watcher.py"


def run(lines, throttle="0"):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "now_playing.json"
        env = dict(os.environ, NOW_PLAYING_PATH=str(out), FREQ="100.7M",
                   RDS_SEEN_WRITE_SEC=throttle)
        subprocess.run([sys.executable, str(WATCHER)], input="\n".join(lines) + "\n",
                       text=True, env=env, check=True, timeout=10)
        return json.loads(out.read_text())


class RdsHeartbeatTest(unittest.TestCase):
    def test_no_groups_leaves_rds_seen_null(self):
        d = run([])
        self.assertIsNone(d["rds_seen"])
        self.assertIsNone(d["pi"])

    def test_decoded_group_sets_rds_seen(self):
        d = run([json.dumps({"pi": "0x211E"})])
        self.assertEqual(d["pi"], "0x211E")
        self.assertIsNotNone(d["rds_seen"])
        self.assertGreaterEqual(d["rds_seen"], d["started_at"])

    def test_unchanged_groups_still_refresh_heartbeat(self):
        # Same PI repeatedly = no content change; heartbeat must still be written.
        grp = json.dumps({"pi": "0x211E", "ps": "KGMO"})
        d = run([grp, grp, grp])
        self.assertIsNotNone(d["rds_seen"])

    def test_garbage_lines_are_not_rds(self):
        d = run(["not json", "{}", json.dumps({"group": "0A"})])
        self.assertIsNone(d["rds_seen"])


if __name__ == "__main__":
    unittest.main()
