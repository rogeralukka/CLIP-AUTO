"""tests/test_phase7.py - Unit test suite for Phase 7 (Live stream monitoring & rolling buffer clipping)."""
import os
import shutil
import subprocess
import tempfile
import unittest

from clipper.live import (
    LiveStreamClipper,
    get_media_duration_seconds,
    get_streamlink_path,
)
from clipper.utils import get_ffmpeg_path


class TestPhase7(unittest.TestCase):
    def test_streamlink_discovery(self):
        # Streamlink should be discovered in the active python virtualenv
        sl_path = get_streamlink_path()
        self.assertIsNotNone(sl_path)
        self.assertTrue(os.path.isfile(sl_path))

    def test_media_duration_measurement(self):
        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
        with tempfile.TemporaryDirectory() as temp_dir:
            test_media = os.path.join(temp_dir, "test_dur.mp4")
            cmd = [
                ffmpeg_bin, "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:a", "aac",
                test_media
            ]
            subprocess.run(cmd, capture_output=True, check=True)

            dur = get_media_duration_seconds(test_media)
            self.assertAlmostEqual(dur, 3.0, delta=0.5)

    def test_live_clipper_buffer_detection_and_dedupe(self):
        test_history_dir = os.path.join("downloads", "live_twitch_httpstwitch.tvtest_channel")
        if os.path.isdir(test_history_dir):
            shutil.rmtree(test_history_dir, ignore_errors=True)
        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # Create simulated live stream buffer: 60s audio with hype spike at 20-25s
                buffer_file = os.path.join(temp_dir, "live_stream.ts")
                filter_complex = (
                    "sine=frequency=440:duration=60,volume='if(between(t,20,25),2.0,0.1)':eval=frame[a];"
                    "testsrc=duration=60:size=1280x720:rate=30[v]"
                )
                cmd = [
                    ffmpeg_bin, "-y",
                    "-filter_complex", filter_complex,
                    "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac",
                    buffer_file
                ]
                subprocess.run(cmd, capture_output=True, check=True)

                # Initialize live clipper pointing to our temp output
                clipper = LiveStreamClipper(
                    stream_url="https://twitch.tv/test_channel",
                    output_base_dir=os.path.join(temp_dir, "live_clips"),
                    use_cpu=True,
                )
                # Override buffer file to our test media
                clipper.buffer_file = buffer_file

                # 1. First check: should detect spike and render clip (bypass LLM judge in synthetic unit test)
                from unittest.mock import patch
                with patch("clipper.live.load_config", return_value={"llm": {"enabled": False}}):
                    clips = clipper.check_and_clip()
                    self.assertGreaterEqual(len(clips), 1)
                    clip = clips[0]
                    self.assertTrue(os.path.isfile(clip["path"]))
                    self.assertGreater(os.path.getsize(clip["path"]), 1024)

                    # Also verify telemetry tracking is functional
                    telem = clipper.get_telemetry()
                    self.assertIn("status", telem)
                    self.assertIn("progress_pct", telem)
                    self.assertGreater(len(telem["recent_events"]), 0)

                    # 2. Second check: should deduplicate against history file and return empty
                    clips_second = clipper.check_and_clip()
                    self.assertEqual(len(clips_second), 0)
        finally:
            if os.path.isdir(test_history_dir):
                shutil.rmtree(test_history_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
