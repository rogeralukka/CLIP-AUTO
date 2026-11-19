"""tests/test_story.py - Unit test suite for Story Hunter and pooled candidate engine."""
import unittest
from unittest.mock import MagicMock, patch

from clipper.story import (
    calculate_virality_score,
    compute_visual_motion_energy,
    dedupe_and_merge_candidates,
    format_indexed_transcript,
    snap_word_indices_to_sentences,
)


class TestStoryHunter(unittest.TestCase):
    def test_virality_score_weights(self):
        # 0.30 * 10 + 0.25 * 8 + 0.30 * 9 + 0.15 * 6 = 3.0 + 2.0 + 2.7 + 0.9 = 8.6
        score = calculate_virality_score(hook=10.0, flow=8.0, emotion=9.0, standalone=6.0)
        self.assertAlmostEqual(score, 8.6, places=2)

    def test_format_indexed_transcript(self):
        words = [
            {"word": "Bro", "start": 0.0, "end": 0.3},
            {"word": "look", "start": 0.4, "end": 0.7},
            {"word": "at", "start": 0.8, "end": 0.9},
            {"word": "this", "start": 1.0, "end": 1.2},
        ]
        indexed = format_indexed_transcript(words)
        self.assertEqual(indexed, "[0] Bro [1] look [2] at [3] this")

    def test_snap_word_indices_to_sentences(self):
        words = [
            {"word": "hello.", "start": 10.0, "end": 10.5},
            {"word": "This", "start": 11.0, "end": 11.3},
            {"word": "game", "start": 11.4, "end": 11.8},
            {"word": "is", "start": 11.9, "end": 12.0},
            {"word": "cinema!", "start": 12.1, "end": 12.6},
            {"word": "Next", "start": 13.5, "end": 13.9},
        ]
        # Start at index 2 ("game"), end at index 4 ("cinema!")
        snapped_s, snapped_e, text = snap_word_indices_to_sentences(words, start_idx=2, end_idx=4)
        # Should snap start backwards to "This" (11.0s)
        self.assertEqual(snapped_s, 11.0)
        # Should snap end forwards to "cinema!" (12.6s) + 0.40s reaction beat = 13.0s
        self.assertEqual(snapped_e, 13.0)
        self.assertEqual(text, "This game is cinema!")

    def test_dedupe_and_merge_candidates(self):
        candidates = [
            {"start": 10.0, "end": 40.0, "score": 6.5},
            {"start": 12.0, "end": 38.0, "score": 8.9},  # Heavily overlapping, higher score
            {"start": 100.0, "end": 130.0, "score": 7.0},
        ]
        merged = dedupe_and_merge_candidates(candidates, overlap_threshold=0.50)
        self.assertEqual(len(merged), 2)
        # Kept the 8.9 score moment
        scores = [c["score"] for c in merged]
        self.assertIn(8.9, scores)
        self.assertIn(7.0, scores)

    def test_compute_visual_motion_energy_nonexistent_file(self):
        per_sec, z_scores = compute_visual_motion_energy("non_existent_file.mp4")
        self.assertEqual(len(per_sec), 0)
        self.assertEqual(len(z_scores), 0)

    def test_hourly_quota_budget_ten_per_streamer(self):
        """Verify that a 10-minute slice does not artificially choke candidates when max_clips_per_hour=10."""
        from clipper.story import hunt_story_candidates_and_pool

        # Mock dependencies of hunt_story_candidates_and_pool
        with patch("clipper.story.transcribe_full_audio") as mock_trans, \
             patch("clipper.story.is_ollama_available", return_value=(True, ["qwen3:14b"])), \
             patch("clipper.story.hunt_story_candidates_in_window") as mock_hunt, \
             patch("clipper.story.extract_action_candidates", return_value=[]):

            words = [{"word": f"word{i}", "start": float(i), "end": float(i) + 0.5} for i in range(25)]
            mock_trans.return_value = {"words": words}
            mock_hunt.return_value = [
                {"start": 10.0, "end": 40.0, "score": 8.5, "payoff_inside": True, "sub_scores": {"standalone": 8.0}},
                {"start": 120.0, "end": 150.0, "score": 7.5, "payoff_inside": True, "sub_scores": {"standalone": 7.0}},
                {"start": 300.0, "end": 330.0, "score": 6.5, "payoff_inside": True, "sub_scores": {"standalone": 6.0}},
            ]

            # 1. With max_clips_per_hour = 10, all 3 moments in this 10-minute (600s) slice must be accepted!
            res = hunt_story_candidates_and_pool(
                slice_wav="dummy.wav",
                slice_duration=600.0,
                max_clips_per_hour=10,
            )
            accepted = [c for c in res if c.get("status") == "accepted"]
            self.assertEqual(len(accepted), 3, "All 3 moments should be accepted when under 10/hr cap")

            # 2. When remaining_quota is 1 and remaining_buffer_quota is 1 (overflow buffer active):
            # Candidate 1 (score 8.5) takes base quota.
            # Candidate 2 (score 7.5 >= 6.5) is rescued by the great-moment buffer!
            # Candidate 3 (score 6.5) is rejected as buffer slot limit (1) was reached.
            res_capped = hunt_story_candidates_and_pool(
                slice_wav="dummy.wav",
                slice_duration=600.0,
                max_clips_per_hour=10,
                remaining_quota=1,
                remaining_buffer_quota=1,
                buffer_score_threshold=7.0,
            )
            accepted_capped = [c for c in res_capped if c.get("status") == "accepted"]
            rejected_capped = [c for c in res_capped if c.get("status") == "rejected"]
            self.assertEqual(len(accepted_capped), 2, "Base quota + 1 buffer clip should be accepted")
            self.assertEqual(len(rejected_capped), 1)
            self.assertTrue("Great-Moment Buffer" in accepted_capped[1]["decision_reason"])
            self.assertTrue("Exceeded hourly quota cap" in rejected_capped[0]["decision_reason"])


if __name__ == "__main__":
    unittest.main()
