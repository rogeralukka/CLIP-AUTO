"""tests/test_phase3.py - Unit test suite for Phase 3 (LLM Judge JSON validation, score blending, graceful fallback)."""
import unittest
from unittest.mock import patch

from clipper.judge import validate_judge_response, rank_moments_with_llm


class TestPhase3(unittest.TestCase):
    def test_json_validation_valid(self):
        sample = '''
        {
          "score": 8.5,
          "standalone": true,
          "payoff_inside": true,
          "hook_title": "Unbelievable 1v4 Clutch Win",
          "reason": "Clear high stakes situation with immediate payoff."
        }
        '''
        res = validate_judge_response(sample)
        self.assertIsNotNone(res)
        self.assertEqual(res["score"], 8.5)
        self.assertTrue(res["standalone"])
        self.assertEqual(res["hook_title"], "Unbelievable 1v4 Clutch Win")

    def test_json_validation_wrapped_markdown(self):
        sample = '''```json
        {
          "score": 7.0,
          "standalone": false,
          "payoff_inside": true,
          "hook_title": "This is a very long title that has way more than seven words in it and should get clamped",
          "reason": "Funny moment."
        }
        ```'''
        res = validate_judge_response(sample)
        self.assertIsNotNone(res)
        self.assertEqual(res["score"], 7.0)
        # Verify word clamping to 7 words
        self.assertLessEqual(len(res["hook_title"].split()), 7)

    def test_json_validation_invalid(self):
        self.assertIsNone(validate_judge_response("Not json at all"))
        self.assertIsNone(validate_judge_response('{"standalone": true}'))  # missing score

    @patch("clipper.judge.load_config")
    def test_silent_fallback_when_disabled(self, mock_cfg):
        mock_cfg.return_value = {"llm": {"enabled": False}}
        moments = [
            {"start": 10.0, "end": 25.0, "score": 3.5},
            {"start": 40.0, "end": 55.0, "score": 2.8},
        ]
        # Should return moments unchanged when LLM is disabled or Ollama is offline
        ranked = rank_moments_with_llm(moments, top_n=2)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0]["start"], 10.0)

    @patch("clipper.judge.is_ollama_available")
    @patch("clipper.judge.call_ollama_generate")
    @patch("clipper.judge.load_config")
    def test_mock_llm_reranking(self, mock_cfg, mock_gen, mock_avail):
        mock_cfg.return_value = {
            "llm": {
                "enabled": True,
                "endpoint": "http://localhost:11434",
                "model": "qwen2.5:7b",
                "candidate_multiplier": 3,
                "weight_llm": 0.50,
                "weight_hype": 0.50,
                "timeout_seconds": 5,
            }
        }
        mock_avail.return_value = (True, ["qwen2.5:7b"])
        # Moment 1 gets low LLM score, Moment 2 gets high LLM score
        mock_gen.side_effect = [
            '{"score": 2.0, "standalone": false, "payoff_inside": false, "hook_title": "Boring", "reason": "No context."}',
            '{"score": 9.5, "standalone": true, "payoff_inside": true, "hook_title": "Insane Double Headshot", "reason": "Great action."}',
        ]

        moments = [
            {"start": 10.0, "end": 25.0, "score": 3.5},
            {"start": 40.0, "end": 55.0, "score": 3.0},
        ]

        ranked = rank_moments_with_llm(moments, top_n=2)
        self.assertEqual(len(ranked), 2)
        # Moment 2 should rank first due to higher blended score
        self.assertEqual(ranked[0]["start"], 40.0)
        self.assertEqual(ranked[0]["hook_title"], "Insane Double Headshot")


if __name__ == "__main__":
    unittest.main()
