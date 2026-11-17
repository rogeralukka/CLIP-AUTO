"""tests/test_phase5.py - Unit test suite for Phase 5 (Hook titles, Loudnorm, Silence trimming, QC, and Platform metadata)."""
import json
import os
import subprocess
import tempfile
import unittest

from clipper.captions import extract_hook_phrase, make_ass
from clipper.cutter import get_loudnorm_audio_filter, render_vertical_clip
from clipper.qc import generate_platform_metadata, perform_qc_check
from clipper.utils import get_ffmpeg_path


class TestPhase5(unittest.TestCase):
    def test_extract_hook_phrase(self):
        # 1. Punctuation cut
        words1 = [
            (0.0, 0.4, "WHAT"),
            (0.4, 0.8, "A"),
            (0.8, 1.2, "PLAY!"),
            (1.2, 1.6, "HE"),
            (1.6, 2.0, "DID"),
            (2.0, 2.4, "IT"),
        ]
        hook1 = extract_hook_phrase(words1)
        self.assertEqual(hook1, "WHAT A PLAY!")

        # 2. Length limit clamp (first 7 words)
        words2 = [(float(i) * 0.3, float(i + 1) * 0.3, f"word{i}") for i in range(12)]
        hook2 = extract_hook_phrase(words2, max_words=5)
        self.assertEqual(len(hook2.split()), 5)

        # 3. Empty words fallback
        self.assertEqual(extract_hook_phrase([]), "")

    def test_hook_title_in_ass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ass_path = os.path.join(temp_dir, "test_hook.ass")
            words = [(0.0, 1.0, "HELLO"), (1.0, 2.0, "WORLD")]
            make_ass(
                words,
                ass_path,
                hook_title="Unbelievable Clutch",
                hook_duration=2.5,
                layout="split",
            )

            self.assertTrue(os.path.isfile(ass_path))
            with open(ass_path, "r", encoding="utf-8-sig") as f:
                content = f.read()

            self.assertIn("Style: Hook", content)
            self.assertIn("UNBELIEVABLE\\NCLUTCH", content)  # long hooks wrap to fit the frame
            self.assertIn("0:00:02.50", content)

    def test_loudnorm_filter_generation(self):
        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = os.path.join(temp_dir, "quiet_sine.mp4")
            # Generate quiet 2s sine wave (-28 LUFS approx)
            cmd = [
                ffmpeg_bin, "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-af", "volume=0.1",
                "-c:a", "aac",
                audio_path
            ]
            subprocess.run(cmd, capture_output=True, check=True)

            flt = get_loudnorm_audio_filter(audio_path, target_lufs=-14.0)
            self.assertIsNotNone(flt)
            self.assertIn("measured_I=", flt)
            self.assertIn("measured_TP=", flt)

    def test_platform_metadata_generation(self):
        campaign = {
            "name": "gta6_creators",
            "required_hashtags": ["GTA6", "GamingShorts"],
            "disclosure_text": "#ad Sponsored by Rockstar",
        }
        meta = generate_platform_metadata(
            hook_title="Insane Helicopter Stunt in Vice City",
            transcript="You won't believe what just happened during the stream.",
            campaign=campaign,
        )

        # YouTube Shorts: Title < 100 chars, includes #Shorts and required hashtags
        self.assertLessEqual(len(meta["youtube_title"]), 100)
        self.assertIn("#Shorts", meta["youtube_title"])
        self.assertIn("#GTA6", meta["youtube_title"])

        # TikTok: Caption < 150 chars
        self.assertLessEqual(len(meta["tiktok_caption"]), 150)
        self.assertIn("#GTA6", meta["tiktok_caption"])
        self.assertIn("#ad", meta["tiktok_caption"])

        # Instagram Reels
        self.assertIn("#GTA6", meta["reels_caption"])
        self.assertIn("Vice City", meta["reels_caption"])

        # X (Twitter): < 280 chars
        self.assertLessEqual(len(meta["x_tweet"]), 280)
        self.assertIn("#ad", meta["x_tweet"])

    def test_qc_check_normal_clip(self):
        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
        with tempfile.TemporaryDirectory() as temp_dir:
            clip_path = os.path.join(temp_dir, "good_clip.mp4")
            cmd = [
                ffmpeg_bin, "-y",
                "-f", "lavfi", "-i", "testsrc=duration=2:size=1080x1920:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-c:a", "aac",
                clip_path
            ]
            subprocess.run(cmd, capture_output=True, check=True)

            report = perform_qc_check(
                clip_path,
                duration=20.0,
                campaign={"min_length_seconds": 15.0, "max_length_seconds": 60.0},
                word_count=25,
            )

            self.assertIn(report["status"], ["PASS", "WARN"])
            self.assertTrue(report["duration_ok"])
            self.assertTrue(report["black_frames_ok"])
            self.assertTrue(report["frozen_frames_ok"])


if __name__ == "__main__":
    unittest.main()
