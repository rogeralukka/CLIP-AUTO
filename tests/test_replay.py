"""tests/test_replay.py - Unit tests for offline replay harness."""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from clipper.replay import (
    correlate_with_feedback,
    extract_audio_slice,
    format_comparison_table,
    parse_timestamp_from_filename,
    replay_slice,
)


class TestReplay(unittest.TestCase):
    def test_parse_timestamp_from_filename(self):
        self.assertEqual(parse_timestamp_from_filename("live_yt_qSyo5_053118_15802s.mp4"), 15802.0)
        self.assertEqual(parse_timestamp_from_filename("rough_clip_120s"), 120.0)
        self.assertIsNone(parse_timestamp_from_filename("no_timestamp_here.mp4"))

    def test_correlate_with_feedback_empty(self):
        res = correlate_with_feedback([], start_sec=0.0, length_sec=1800.0, source_file="dummy.ts")
        self.assertIn("total_user_ratings_in_slice", res)

    @patch("clipper.replay.load_creator_feedback")
    def test_correlate_with_feedback_matching(self, mock_load):
        mock_load.return_value = [
            {
                "filename": "live_yt_15802s.mp4",
                "rating": 5,
                "tag": "viral",
            },
            {
                "filename": "live_yt_15900s.mp4",
                "rating": 1,
                "tag": "hollow",
            }
        ]
        candidates = [
            {
                "start": 2.0,  # relative to slice start 15800 -> 15802
                "end": 35.0,
                "score": 8.0,
                "status": "accepted",
            },
            {
                "start": 100.0,  # relative to slice start 15800 -> 15900
                "end": 130.0,
                "score": 3.0,
                "status": "rejected",
            }
        ]
        res = correlate_with_feedback(candidates, start_sec=15800.0, length_sec=300.0, source_file="dummy.ts")
        self.assertEqual(res["total_user_ratings_in_slice"], 2)
        self.assertEqual(res["matched_candidates"], 2)
        self.assertEqual(res["recall"], 1.0)
        self.assertEqual(res["alignment_rate"], 1.0)

    def test_format_comparison_table(self):
        old_cands = [
            {"start": 10.0, "end": 40.0, "score": 4.0, "status": "rejected", "decision_reason": "Low score"}
        ]
        new_cands = [
            {"start": 12.0, "end": 42.0, "score": 8.5, "status": "accepted", "hook_title": "Huge Clutch"}
        ]
        table = format_comparison_table(old_cands, new_cands, start_sec=100.0)
        self.assertIn("ENGINE COMPARISON SUMMARY", table)
        self.assertIn("Huge Clutch", table)
        self.assertIn("Low score", table)


if __name__ == "__main__":
    unittest.main()
