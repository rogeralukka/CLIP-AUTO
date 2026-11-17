"""tests/test_phase6.py - Unit test suite for Phase 6 (Multi-link queueing, Review-step updates, Gallery layout re-rendering)."""
import os
import subprocess
import tempfile
import unittest

from clipper.cutter import render_vertical_clip
from clipper.qc import generate_platform_metadata, perform_qc_check
from clipper.queue import add_job, confirm_render_moments, load_jobs, mutate_job
from clipper.utils import get_ffmpeg_path


class TestPhase6(unittest.TestCase):
    def tearDown(self):
        from clipper.queue import cancel_all, clear_finished_jobs
        cancel_all()
        clear_finished_jobs()

    def test_multi_link_queueing(self):
        links_text = """
        https://www.twitch.tv/videos/1234567890
        https://www.twitch.tv/videos/1234567891
        https://youtube.com/watch?v=abcdefghijk
        """
        raw_lines = [l.strip() for l in links_text.strip().splitlines() if l.strip()]
        self.assertEqual(len(raw_lines), 3)

        queued_jids = []
        for url in raw_lines:
            jid = add_job(vod_input=url, quality="1080p")
            queued_jids.append(jid)

        self.assertEqual(len(queued_jids), 3)
        jobs = load_jobs()
        for jid in queued_jids:
            self.assertIn(jid, jobs)
            self.assertIn(jobs[jid]["status"], ["QUEUED", "RUNNING"])

    def test_review_step_hook_title_update(self):
        jid = add_job(vod_input="https://www.twitch.tv/videos/test_review_job")
        test_moments = [
            {"start": 10.0, "end": 35.0, "score": 3.2, "preview": "nice clutch", "hook_title": "Original Title"},
            {"start": 40.0, "end": 65.0, "score": 2.8, "preview": "funny death", "hook_title": "Death"},
        ]

        def _set_awaiting(j):
            j["status"] = "AWAITING_SELECTION"
            j["detected_moments"] = test_moments

        mutate_job(jid, _set_awaiting)

        # Update hook title during review
        test_moments[0]["hook_title"] = "Updated Clutch God Title"
        confirm_render_moments(jid, selected_indices=[0], updated_moments=test_moments)

        jobs = load_jobs()
        self.assertEqual(jobs[jid]["status"], "QUEUED")
        self.assertEqual(jobs[jid]["selected_indices"], [0])
        self.assertEqual(jobs[jid]["detected_moments"][0]["hook_title"], "Updated Clutch God Title")

    def test_single_clip_layout_rerendering(self):
        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
        with tempfile.TemporaryDirectory() as temp_dir:
            rough_path = os.path.join(temp_dir, "rough_01.mp4")
            clip_path = os.path.join(temp_dir, "clip_01.mp4")

            # Generate 1s 1920x1080 rough video
            cmd = [
                ffmpeg_bin, "-y",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=1920x1080:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-c:a", "aac",
                rough_path
            ]
            subprocess.run(cmd, capture_output=True, check=True)

            # 1. Initial render with gameplay layout
            render_vertical_clip(rough_path, None, clip_path, layout="gameplay", use_cpu=True)
            self.assertTrue(os.path.isfile(clip_path))
            size1 = os.path.getsize(clip_path)
            self.assertGreater(size1, 1024)

            # 2. Re-render with split layout
            render_vertical_clip(rough_path, None, clip_path, layout="split", use_cpu=True)
            self.assertTrue(os.path.isfile(clip_path))
            size2 = os.path.getsize(clip_path)
            self.assertGreater(size2, 1024)

            # 3. Verify QC on re-rendered clip
            qc = perform_qc_check(clip_path, duration=20.0, layout="split")
            self.assertIn(qc["status"], ["PASS", "WARN"])


if __name__ == "__main__":
    unittest.main()
