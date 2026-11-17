"""tests/test_phase2.py - Unit test suite for Phase 2 (Hype curve, Robust z-score, Keywords, Boundaries, Dedupe)."""
import os
import unittest
import numpy as np

from clipper.signals import (
    compute_robust_z_scores,
    calculate_chat_signals,
    load_hype_keywords,
    snap_moment_boundaries,
    dedupe_moments,
)


class TestPhase2(unittest.TestCase):
    def test_robust_z_scores(self):
        # 100 seconds of baseline zeros with a sudden burst at t=50
        values = np.zeros(100, dtype=np.float32)
        values[50] = 20.0
        values[51] = 25.0

        z_scores = compute_robust_z_scores(values, window_size=40)
        self.assertEqual(len(z_scores), 100)
        self.assertGreater(z_scores[50], 3.0, "Spike at t=50 should have high robust z-score")
        self.assertAlmostEqual(z_scores[10], 0.0, places=2, msg="Baseline values should have zero z-score")

    def test_keyword_and_pattern_scoring(self):
        comments = [
            {"content_offset_seconds": 10.0, "message": {"body": "CLIP IT BRO NO WAY KEKW"}},
            {"content_offset_seconds": 10.5, "message": {"body": "WHAT IS HAPPENING ????"}},
            {"content_offset_seconds": 11.0, "message": {"body": "LETS GOOOOOOOOOO"}},
            {"content_offset_seconds": 11.5, "message": {"body": "w w w w"}},
        ]
        chat_rate, chat_kw, msgs_by_sec = calculate_chat_signals(comments, duration=30)
        self.assertEqual(chat_rate[10], 2.0)
        self.assertGreater(chat_kw[10], 5.0, "Keywords 'CLIP IT', 'NO WAY', 'KEKW' should score high")
        self.assertGreater(chat_kw[11], 2.0, "Repeated 'w' and all caps should score")

    def test_smart_boundary_snapping(self):
        full_transcript = {
            "duration": 120.0,
            "segments": [
                {"start": 18.2, "end": 22.0, "text": "He is turning around the corner."},
                {"start": 22.5, "end": 28.1, "text": "Unbelievable headshot right there!"},
                {"start": 28.5, "end": 35.0, "text": "That was insane."}
            ]
        }
        # Peak at t=24.0, min_len=15, max_lead=5.0
        st, et = snap_moment_boundaries(
            peak_time=24.0,
            stream_duration=120.0,
            full_transcript_data=full_transcript,
            min_length=15.0,
            max_length=60.0,
            max_lead=5.0,
        )
        self.assertLessEqual(st, 20.0, "Start should snap near or before t=20")
        self.assertGreaterEqual(et - st, 15.0, "Duration must satisfy minimum length")

    def test_deduplication(self):
        test_vod_id = "test_vod_dedupe"
        test_file = os.path.join("downloads", test_vod_id, "clipped_moments.json")
        if os.path.isfile(test_file):
            try:
                os.remove(test_file)
            except Exception:
                pass

        moments = [
            {"start": 10.0, "end": 30.0, "score": 4.5},
            # 80% overlap with moment 1 (from t=12 to t=28) -> should be discarded
            {"start": 12.0, "end": 28.0, "score": 3.8},
            # Disjoint moment (t=60 to t=80) -> should be kept
            {"start": 60.0, "end": 80.0, "score": 4.0},
        ]

        deduped = dedupe_moments(moments, vod_id=test_vod_id, overlap_threshold=0.50)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]["start"], 10.0)
        self.assertEqual(deduped[1]["start"], 60.0)

        # Cleanup
        if os.path.isfile(test_file):
            os.remove(test_file)


if __name__ == "__main__":
    unittest.main()
