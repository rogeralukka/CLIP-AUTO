"""tests/test_phase1.py - Unit test suite for Phase 1 (Audio-first, Kick detection, Transcript slicing)."""
import os
import unittest
from clipper.downloader import is_kick, is_twitch, is_youtube, get_vod_id
from clipper.transcription import slice_transcript, get_transcript_text_in_window, unload_whisper_model


class TestPhase1(unittest.TestCase):
    def test_url_and_vod_id_detection(self):
        # Twitch
        tw_url = "https://www.twitch.tv/videos/123456789"
        self.assertTrue(is_twitch(tw_url))
        self.assertFalse(is_kick(tw_url))
        self.assertEqual(get_vod_id(tw_url), "twitch_123456789")

        # YouTube
        yt_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertTrue(is_youtube(yt_url))
        self.assertFalse(is_kick(yt_url))
        self.assertEqual(get_vod_id(yt_url), "yt_dQw4w9WgXcQ")

        # Kick
        kick_url = "https://kick.com/video/550e8400-e29b-41d4-a716-446655440000"
        self.assertTrue(is_kick(kick_url))
        self.assertFalse(is_twitch(kick_url))
        self.assertFalse(is_youtube(kick_url))
        self.assertEqual(get_vod_id(kick_url), "kick_550e8400-e29b-41d4-a716-446655440000")

        # Local
        loc = r"C:\videos\my_stream.mp4"
        self.assertTrue(get_vod_id(loc).startswith("local_my_stream"))

    def test_transcript_slicing(self):
        full_transcript = {
            "source": "dummy.m4a",
            "segments": [
                {"start": 10.0, "end": 15.0, "text": "Hello world this is a test"},
                {"start": 20.0, "end": 25.0, "text": "Hype moment happens right now"}
            ],
            "words": [
                {"start": 10.0, "end": 10.5, "word": "Hello"},
                {"start": 10.6, "end": 11.2, "word": "world"},
                {"start": 20.0, "end": 20.8, "word": "Hype"},
                {"start": 21.0, "end": 21.9, "word": "moment"},
                {"start": 22.0, "end": 22.9, "word": "happens"},
                {"start": 30.0, "end": 30.5, "word": "Later"}
            ]
        }

        # Slice moment between t=19.5 and t=23.5 (duration = 4.0s)
        sliced = slice_transcript(full_transcript, start_sec=19.5, end_sec=23.5)
        self.assertEqual(len(sliced), 3)

        # First sliced word is "Hype": relative start should be 20.0 - 19.5 = 0.5
        w1_start, w1_end, w1_text = sliced[0]
        self.assertEqual(w1_text, "Hype")
        self.assertAlmostEqual(w1_start, 0.5, places=2)
        self.assertAlmostEqual(w1_end, 1.3, places=2)

        # Window text
        window_txt = get_transcript_text_in_window(full_transcript, 19.5, 23.5)
        self.assertEqual(window_txt, "Hype moment happens")

    def test_unload_whisper(self):
        # Should execute safely without errors
        unload_whisper_model()


if __name__ == "__main__":
    unittest.main()
