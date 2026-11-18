"""tests/test_live_cams.py - Live mode uses the same cam logic as normal runs."""
import os
import tempfile
import unittest
from unittest.mock import patch

from clipper import live


CAM = {"box": [1400, 700, 400, 300], "presence": 0.9, "corner": "bottom-right"}


class TestLiveCams(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.lc = live.LiveStreamClipper("https://www.twitch.tv/somebody")

    def tearDown(self):
        os.chdir(self.cwd)

    def test_default_quality_prefers_1080(self):
        self.assertTrue(self.lc.quality.startswith("1080p60"))

    def test_probe_learns_cams_before_first_clip(self):
        info = {"cams": [CAM], "found": True, "layout": "split"}
        with patch.object(live, "detect_facecam", return_value=info) as det, \
                patch.object(live, "merge_known_cams", side_effect=lambda k, c: list(c)):
            self.lc._probe_cams(300.0)
        self.assertEqual(len(self.lc.known_cams), 1)
        self.assertTrue(os.path.isfile(self.lc.cams_file))
        kw = det.call_args.kwargs
        self.assertEqual(kw["sample_interval"], 4.0)
        self.assertAlmostEqual(kw["end_sec"] - kw["start_sec"], 150.0)

    def test_probe_failure_does_not_stop_clipping(self):
        with patch.object(live, "detect_facecam", side_effect=RuntimeError("boom")):
            self.lc._probe_cams(300.0)   # must not raise
        self.assertEqual(self.lc.known_cams, [])

    def test_probe_skipped_when_cams_known(self):
        self.lc.known_cams = [CAM]
        self.lc._checks = 1   # next call is check 2 -> not a refresh
        with patch.object(live, "detect_facecam") as det:
            self.lc._probe_cams(300.0)
        det.assert_not_called()

    def test_second_look_rerenders_clip_that_missed_a_cam(self):
        rough = os.path.join(self.tmp, "rough.mp4")
        open(rough, "wb").write(b"x")
        meta = os.path.join(self.tmp, "m.json")
        self.lc.known_cams = [CAM]
        self.lc.clips_done = [{"filename": "a.mp4", "rough_path": rough, "facecam": {"cams": []}, "start_seconds": 1,
                               "end_seconds": 20, "path": "a.mp4", "meta_path": meta}]
        new_info = {"cams": [CAM], "layout": "split"}
        out = {"filename": "a.mp4", "meta_path": meta, "facecam": new_info, "layout": "split"}
        with patch.object(live, "detect_facecam", return_value=new_info), \
                patch("clipper.pipeline.rerender_clip", return_value=out) as rr:
            self.lc._second_look()
        rr.assert_called_once()
        self.assertEqual(self.lc.clips_done[0]["layout"], "split")
        self.assertTrue(os.path.isfile(meta))

    def test_second_look_leaves_clip_alone_when_nothing_new(self):
        rough = os.path.join(self.tmp, "r.mp4")
        open(rough, "wb").write(b"x")
        self.lc.known_cams = [CAM]
        self.lc.clips_done = [{"filename": "a.mp4", "rough_path": rough, "facecam": {"cams": []}, "start_seconds": 1,
                               "end_seconds": 20, "path": "a.mp4", "meta_path": "x"}]
        with patch.object(live, "detect_facecam", return_value={"cams": []}), \
                patch("clipper.pipeline.rerender_clip") as rr:
            self.lc._second_look()
        rr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
