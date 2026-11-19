"""tests/test_live_duration_and_cache.py - Test minimum duration floor (30s) and transcript cache invalidation."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from clipper import live
from clipper.transcription import transcribe_full_audio
from clipper.config import load_config


class TestLiveDurationAndCache(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)

    def test_default_min_clip_length_is_30(self):
        cfg = load_config()
        min_clip_len = float(cfg.get("signals", {}).get("default_min_clip_length", 30.0))
        self.assertEqual(min_clip_len, 30.0)

    def test_transcript_cache_invalidated_when_audio_newer(self):
        audio_file = os.path.join(self.tmp, "test_audio.wav")
        json_file = os.path.join(self.tmp, "test_audio_transcript.json")

        # Write audio first
        with open(audio_file, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 100)

        # Write stale cache with earlier time
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump({"words": [{"word": "old", "start": 0.0, "end": 1.0}]}, f)

        # Ensure json mtime is older than audio mtime
        old_time = time.time() - 100.0
        os.utime(json_file, (old_time, old_time))
        new_time = time.time()
        os.utime(audio_file, (new_time, new_time))

        # Mock whisper so we can see if it was called because cache was invalidated
        mock_model = MagicMock()
        mock_segment = MagicMock()
        mock_segment.words = [MagicMock(word="new", start=0.0, end=1.0, probability=0.9)]
        mock_model.transcribe.return_value = ([mock_segment], MagicMock(duration=1.0, language="en", language_probability=1.0))

        with patch("clipper.transcription.get_whisper_model", return_value=mock_model):
            res = transcribe_full_audio(audio_file, output_json=json_file)
            mock_model.transcribe.assert_called_once()
            self.assertEqual(res["words"][0]["word"], "new")

    def test_transcript_cache_used_when_audio_older(self):
        audio_file = os.path.join(self.tmp, "test_audio.wav")
        json_file = os.path.join(self.tmp, "test_audio_transcript.json")

        with open(audio_file, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 100)

        with open(json_file, "w", encoding="utf-8") as f:
            json.dump({"words": [{"word": "cached_word", "start": 0.0, "end": 1.0}]}, f)

        # Audio is older than JSON
        old_time = time.time() - 100.0
        os.utime(audio_file, (old_time, old_time))
        new_time = time.time()
        os.utime(json_file, (new_time, new_time))

        mock_model = MagicMock()
        with patch("clipper.transcription.get_whisper_model", return_value=mock_model):
            res = transcribe_full_audio(audio_file, output_json=json_file)
            mock_model.transcribe.assert_not_called()
            self.assertEqual(res["words"][0]["word"], "cached_word")
