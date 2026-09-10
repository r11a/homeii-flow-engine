from pathlib import Path
import runpy
import unittest
pair = runpy.run_path(str(Path(__file__).resolve().parents[1] / "custom_components/homeii_flow/player_timing.py"))["playback_position_pair"]

class PlayerTimingTests(unittest.TestCase):
    def test_current_media_clock_replaces_stale_wiim_clock(self):
        raw = {"elapsed_time":0,"elapsed_time_last_updated":1788988205,"current_media":{"elapsed_time":2,"elapsed_time_last_updated":1788992080}}
        position, stamp = pair(raw)
        self.assertEqual(position, 2)
        self.assertEqual(stamp, "2026-09-09T22:14:40+00:00")
        raw["current_media"]["elapsed_time"] = 0
        self.assertEqual(pair(raw)[0], 0)

    def test_iso_and_milliseconds_keep_position_from_matching_snapshot(self):
        raw = {"elapsed_time":14,"elapsed_time_last_updated":1788992090000,"current_media":{"elapsed_time":2,"elapsed_time_last_updated":"2026-09-09T22:14:40Z"}}
        self.assertEqual(pair(raw), (14, "2026-09-09T22:14:50+00:00"))
        self.assertEqual(pair({"elapsed_time":float("nan")}), (0, None))
