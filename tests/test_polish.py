"""tests/test_polish.py - Unit test suite for Step 4 Polish (punch-zoom & dead-air trimming)."""
import os
import subprocess
import tempfile
import unittest

from clipper.cutter import (
    build_punch_zoom_filter,
    compress_internal_dead_air,
)
from clipper.utils import get_ffmpeg_path


class TestPolish(unittest.TestCase):
    def test_build_punch_zoom_filter(self):
        # 1. Normal clip duration (15s): should generate valid FFmpeg crop/scale filter
        filt = build_punch_zoom_filter(clip_duration=15.0, zoom_factor=1.12)
        self.assertIsNotNone(filt)
        self.assertIn("crop=w='if(between(t,11.00,14.50)", filt)
        self.assertIn("1.120", filt)
        self.assertIn("scale=1080:1920:flags=lanczos", filt)

        # 2. Custom punch timestamps
        filt_custom = build_punch_zoom_filter(
            clip_duration=20.0,
            punch_start_sec=14.0,
            punch_end_sec=18.0,
            zoom_factor=1.15,
        )
        self.assertIsNotNone(filt_custom)
        self.assertIn("between(t,14.00,18.00)", filt_custom)
        self.assertIn("1.150", filt_custom)

        # 3. Clip too short (< 6s): should return None
        filt_short = build_punch_zoom_filter(clip_duration=4.5)
        self.assertIsNone(filt_short)

    def test_compress_internal_dead_air_math_and_retime(self):
        """Test silence gap detection and exact subtitle retiming."""
        # 3 words:
        # Word 0: 0.0s - 1.0s ("Hello")
        # Word 1: 2.2s - 3.0s ("world") -> gap is 1.2s (> 0.40s threshold).
        # Target gap is 0.15s, so excess to cut is 1.2 - 0.15 = 1.05s.
        # Word 2: 3.2s - 4.0s ("stream") -> gap is 0.2s (<= 0.40s, keep).
        words = [
            {"start": 0.0, "end": 1.0, "word": "Hello"},
            {"start": 2.2, "end": 3.0, "word": "world"},
            {"start": 3.2, "end": 4.0, "word": "stream"},
        ]

        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
        with tempfile.TemporaryDirectory() as temp_dir:
            dummy_rough = os.path.join(temp_dir, "rough.mp4")
            dummy_out = os.path.join(temp_dir, "rough_tight.mp4")

            # Generate a 5-second test video
            cmd_gen = [
                ffmpeg_bin, "-y",
                "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac",
                dummy_rough,
            ]
            subprocess.run(cmd_gen, capture_output=True, check=True)

            retimed, out_path = compress_internal_dead_air(
                words=words,
                rough_path=dummy_rough,
                output_path=dummy_out,
                min_gap_sec=0.40,
                target_gap_sec=0.15,
                use_cpu=True,
            )

            self.assertTrue(os.path.isfile(out_path))
            self.assertEqual(len(retimed), 3)

            # Word 0 should be unchanged
            self.assertAlmostEqual(retimed[0]["start"], 0.0, delta=0.01)
            self.assertAlmostEqual(retimed[0]["end"], 1.0, delta=0.01)

            # Word 1 should have start shifted by 1.05s (2.2 - 1.05 = 1.15s)
            self.assertAlmostEqual(retimed[1]["start"], 1.15, delta=0.02)
            self.assertAlmostEqual(retimed[1]["end"], 1.95, delta=0.02)

            # The gap between Word 0 end (1.0s) and Word 1 start (1.15s) is exactly 0.15s!
            gap = retimed[1]["start"] - retimed[0]["end"]
            self.assertAlmostEqual(gap, 0.15, delta=0.02)


if __name__ == "__main__":
    unittest.main()
