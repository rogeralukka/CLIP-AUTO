"""tests/test_live_dedupe.py - Tests for content-based deduplication and PTS void guards in live clipping."""
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from clipper.signals import dedupe_moments, record_clipped_moment
from clipper.live import LiveStreamClipper


class TestLiveDeduplication(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.vod_id = "test_vod_live_dedupe"

    def tearDown(self):
        if os.path.isdir(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_dedupe_rejects_matching_title_at_different_timestamps(self):
        # Record an existing moment at 7200s with title "Lucia or Jason mission debate"
        record_clipped_moment(
            vod_id=self.vod_id,
            start=7253.0,
            end=7319.0,
            score=8.5,
            downloads_dir=self.test_dir,
            hook_title="Lucia or Jason mission debate",
            transcript="to um switch back to uh freak lucia chat",
        )

        # New candidate at completely different timestamp (8027s) but same title
        new_candidates = [
            {
                "start": 8027.0,
                "end": 8117.0,
                "score": 8.6,
                "hook_title": "Lucia or Jason mission debate",
                "transcript": "chat what mission should we do",
            },
            {
                "start": 8200.0,
                "end": 8260.0,
                "score": 9.0,
                "hook_title": "Epic police escape in the sports car",
                "transcript": "we lost the five stars lets go",
            },
        ]

        accepted = dedupe_moments(
            new_moments=new_candidates,
            vod_id=self.vod_id,
            downloads_dir=self.test_dir,
        )

        # The candidate with the same title should be rejected; the distinct moment kept
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["start"], 8200.0)
        self.assertEqual(accepted[0]["hook_title"], "Epic police escape in the sports car")

    def test_dedupe_rejects_high_transcript_overlap_at_different_timestamps(self):
        # Record an existing moment with dialogue
        dialogue = "to um switch back to uh freak lucia chat y all want a lucy mission or a jason mission"
        record_clipped_moment(
            vod_id=self.vod_id,
            start=1000.0,
            end=1060.0,
            score=8.0,
            downloads_dir=self.test_dir,
            hook_title="First Choice",
            transcript=dialogue,
        )

        # New candidate at 5000s (no time overlap, different title, but same spoken words)
        new_candidates = [
            {
                "start": 5000.0,
                "end": 5090.0,
                "score": 8.2,
                "hook_title": "Completely Different Title",
                "transcript": "to um switch back to uh freak lucia chat y all want a lucy mission or um jason",
            },
            {
                "start": 6000.0,
                "end": 6050.0,
                "score": 8.0,
                "hook_title": "Unique Moment",
                "transcript": "driving along the beach enjoying the sunset",
            },
        ]

        accepted = dedupe_moments(
            new_moments=new_candidates,
            vod_id=self.vod_id,
            downloads_dir=self.test_dir,
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["start"], 6000.0)

    def test_stale_buffer_rotation_on_start(self):
        # Create a mock clipper with a buffer file that is 10 minutes old
        buffer_dir = os.path.join(self.test_dir, "test_stream_buffer")
        os.makedirs(buffer_dir, exist_ok=True)
        buf_file = os.path.join(buffer_dir, "live_stream.ts")

        # Write dummy 2MB file
        with open(buf_file, "wb") as f:
            f.write(b"0" * (2 * 1024 * 1024))

        # Set modification time to 600 seconds ago (10 minutes ago)
        stale_time = time.time() - 600.0
        os.utime(buf_file, (stale_time, stale_time))

        clipper = LiveStreamClipper(stream_url="https://youtube.com/live/fake123", output_base_dir=self.test_dir)
        clipper.buffer_dir = buffer_dir
        clipper.buffer_file = buf_file

        with patch("subprocess.Popen") as mock_popen, patch("clipper.live.get_streamlink_path", return_value=None):
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            # Calling start_capture_process should detect stale buffer and rotate it
            clipper.start_capture_process()

        # The old file should have been rotated (not directly appended to)
        prev_files = [f for f in os.listdir(buffer_dir) if f.startswith("live_stream_prev_")]
        self.assertEqual(len(prev_files), 1)
        self.assertEqual(clipper.last_analyzed_time, 0.0)


    def test_tail_audio_hash_guard_skips_pts_void(self):
        # Create a mock clipper
        clipper = LiveStreamClipper(stream_url="https://youtube.com/live/fake123", output_base_dir=self.test_dir)
        clipper.buffer_dir = self.test_dir
        clipper.buffer_file = os.path.join(self.test_dir, "live_stream.ts")
        clipper.last_analyzed_time = 7200.0

        # Create dummy buffer file
        with open(clipper.buffer_file, "wb") as f:
            f.write(b"dummy_stream_content")

        tail_wav = os.path.join(self.test_dir, "tail.wav")
        with open(tail_wav, "wb") as f:
            f.write(b"identical_audio_content" * 100)

        import hashlib
        dummy_hash = hashlib.sha256(open(tail_wav, "rb").read()).hexdigest()
        clipper._last_tail_hash = dummy_hash  # Previous turn recorded this hash

        with patch("subprocess.run") as mock_run, patch("clipper.live.get_media_duration_seconds", return_value=40000.0):
            # check_and_clip should detect that tail.wav is identical to _last_tail_hash and abort without calling story hunter
            with patch("clipper.story.hunt_story_candidates_and_pool") as mock_story:
                result = clipper.check_and_clip()
                self.assertEqual(result, [])
                mock_story.assert_not_called()
                # Should have jumped analyzed time forward past the gap
                self.assertGreater(clipper.last_analyzed_time, 7200.0)


if __name__ == "__main__":
    unittest.main()
