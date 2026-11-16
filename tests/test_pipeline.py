"""Automated unit and integration test suite for the stream clipper pipeline."""
import json
import os
import shutil
import subprocess
import sys
import unittest

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from clipper.captions import DEFAULT_PRESET, escape_ffmpeg_ass_path, load_preset, make_ass
from clipper.cutter import cut_rough_clip, render_vertical_clip
from clipper.detection import detect_audio_loudness_moments, spike_moments
from clipper.downloader import parse_youtube_live_chat
from clipper.pipeline import run_pipeline
from clipper.utils import get_ffmpeg_path, setup_cuda_dlls
from tests.generate_test_assets import generate_synthetic_assets


class TestClipperPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        setup_cuda_dlls()
        cls.test_dir = os.path.join(PROJECT_ROOT, "tests", "test_scratch")
        os.makedirs(cls.test_dir, exist_ok=True)
        cls.video_path, cls.chat_path = generate_synthetic_assets(
            output_dir=os.path.join(cls.test_dir, "data")
        )

    @classmethod
    def tearDownClass(cls):
        if os.path.isdir(cls.test_dir):
            try:
                shutil.rmtree(cls.test_dir)
            except Exception:
                pass

    def test_01_spike_detector_finds_bursts(self):
        """Test chat spike detection accurately locates the 2 simulated hype bursts."""
        with open(self.chat_path, "r", encoding="utf-8") as f:
            comments = json.load(f)["comments"]

        moments = spike_moments(
            comments, window=5, z_thresh=2.0, min_msgs=10, before=5, after=10, top=5
        )
        self.assertGreaterEqual(len(moments), 2, "Spike detector should detect both hype bursts")

        # Verify timestamps overlap the known bursts (t=20 and t=42)
        burst_1_found = any(s <= 22 <= e for s, e, _ in moments)
        burst_2_found = any(s <= 44 <= e for s, e, _ in moments)

        self.assertTrue(burst_1_found, f"Burst 1 (~20s) was not captured in {moments}")
        self.assertTrue(burst_2_found, f"Burst 2 (~42s) was not captured in {moments}")

    def test_02_audio_loudness_fallback(self):
        """Test audio loudness fallback detects peaks when chat is not provided."""
        moments = detect_audio_loudness_moments(
            self.video_path, window=5, z_thresh=1.5, before=5, after=10, top=4
        )
        self.assertGreaterEqual(len(moments), 1, "Audio loudness should detect loud sections")
        # Check that detected moment spans the loud period
        self.assertTrue(
            any(s <= 25 and e >= 20 for s, e, _ in moments) or
            any(s <= 46 and e >= 42 for s, e, _ in moments),
            f"Audio peaks should align with volume spikes, found: {moments}"
        )

    def test_03_caption_presets_and_ass_generation(self):
        """Test karaoke ASS subtitle generation with different styling presets."""
        words = [
            (0.5, 0.9, "welcome"),
            (0.9, 1.3, "to"),
            (1.3, 1.8, "the"),
            (1.8, 2.4, "stream"),
        ]

        for preset_name in ["classic_yellow", "neon_green", "cyber_cyan", "clean_white"]:
            preset = load_preset(preset_name)
            ass_path = os.path.join(self.test_dir, f"test_{preset_name}.ass")
            out_file = make_ass(words, ass_path, preset=preset)

            self.assertTrue(os.path.isfile(out_file), f"ASS file should exist: {out_file}")
            with open(out_file, "r", encoding="utf-8-sig") as f:
                content = f.read()

            self.assertIn("[Script Info]", content)
            self.assertIn("PlayResX: 1080", content)
            self.assertIn("PlayResY: 1920", content)
            self.assertIn("Dialogue: 0,", content)
            self.assertIn(r"{\kf", content, "Subtitle line must contain karaoke timing tag")

    def test_04_windows_path_escaping(self):
        """Test that Windows paths with spaces, colons, and backslashes are escaped safely."""
        path = r"C:\Users\Roger\Documents\EXP\CLIP AUTO\clips\cap_01.ass"
        escaped = escape_ffmpeg_ass_path(path)
        # Verify drive colon is escaped
        self.assertIn(r"C\:", escaped, "Drive colon must be escaped for FFmpeg filters")
        # Verify folder separators are converted to forward slashes
        self.assertIn("/Users/Roger/Documents/EXP/CLIP AUTO/clips/cap_01.ass", escaped)

    def test_05_accurate_cut_and_render_vertical(self):
        """Test accurate cut and 1080x1920 vertical export using NVENC (or CPU fallback)."""
        rough_clip = os.path.join(self.test_dir, "rough_test.mp4")
        cut_rough_clip(self.video_path, start_sec=20.0, end_sec=28.0, output_path=rough_clip)

        self.assertTrue(os.path.isfile(rough_clip), "Rough cut must exist")
        self.assertGreater(os.path.getsize(rough_clip), 1024, "Rough cut must be non-empty")

        # Generate test ASS
        ass_path = os.path.join(self.test_dir, "test_cap.ass")
        make_ass([(0.5, 1.2, "POGGERS"), (1.2, 2.0, "INSANE")], ass_path)

        final_clip = os.path.join(self.test_dir, "final_vertical.mp4")
        render_vertical_clip(rough_clip, ass_path, final_clip)

        self.assertTrue(os.path.isfile(final_clip), "Vertical clip must exist")
        self.assertGreater(os.path.getsize(final_clip), 5000, "Vertical clip must be non-empty")

        # Probe dimensions using ffprobe
        import re
        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
        ffprobe_bin = re.sub(r'ffmpeg\.exe$', 'ffprobe.exe', ffmpeg_bin, flags=re.IGNORECASE)
        if os.path.isfile(ffprobe_bin):
            probe_cmd = [
                ffprobe_bin, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
                final_clip
            ]
            dim = subprocess.check_output(probe_cmd, text=True).strip()
            self.assertEqual(dim, "1080,1920", "Exported clip must be 1080x1920 vertical (9:16)")

    def test_06_youtube_live_chat_parser(self):
        """Test YouTube live chat JSONL parsing into standard comments format."""
        sample_yt_chat = os.path.join(self.test_dir, "sample.live_chat.json")
        sample_lines = [
            json.dumps({
                "replayChatItemAction": {
                    "actions": [{
                        "addChatItemAction": {
                            "item": {
                                "liveChatTextMessageRenderer": {
                                    "message": {"runs": [{"text": "Hello stream!"}]},
                                    "videoOffsetTimeMsec": "1500"
                                }
                            }
                        }
                    }]
                }
            }),
            json.dumps({
                "replayChatItemAction": {
                    "actions": [{
                        "addChatItemAction": {
                            "item": {
                                "liveChatTextMessageRenderer": {
                                    "message": {"runs": [{"text": "POG PLAY"}]},
                                    "videoOffsetTimeMsec": "3200"
                                }
                            }
                        }
                    }]
                }
            })
        ]
        with open(sample_yt_chat, "w", encoding="utf-8") as f:
            f.write("\n".join(sample_lines) + "\n")

        parsed = parse_youtube_live_chat(sample_yt_chat)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["content_offset_seconds"], 1.5)
        self.assertEqual(parsed[0]["message"]["body"], "Hello stream!")
        self.assertEqual(parsed[1]["content_offset_seconds"], 3.2)
        self.assertEqual(parsed[1]["message"]["body"], "POG PLAY")


if __name__ == "__main__":
    unittest.main()
