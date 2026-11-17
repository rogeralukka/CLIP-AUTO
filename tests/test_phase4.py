"""tests/test_phase4.py - Unit test suite for Phase 4 (Facecam detection & auto-layouts)."""
import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from clipper.facecam import (
    FaceDetectorWrapper,
    cluster_face_boxes,
    compute_gameplay_crop,
    detect_facecam,
    expand_box_with_padding,
    get_face_detector,
)
from clipper.cutter import render_vertical_clip
from clipper.utils import get_ffmpeg_path


class TestPhase4(unittest.TestCase):
    def test_detector_initialization(self):
        detector = get_face_detector()
        self.assertIn(detector.backend, ["mediapipe", "haar", "none"])
        # Should be able to process an image without throwing
        dummy = np.zeros((480, 640, 3), dtype=np.uint8)
        boxes = detector.detect_faces(dummy)
        self.assertIsInstance(boxes, list)

    def test_cluster_face_boxes(self):
        # 10 detections clustered in top-right corner (around x=1400, y=100)
        boxes = [
            (float(i), (1400 + i * 2, 100 - i, 200, 200))
            for i in range(10)
        ]
        # 2 outlier boxes in bottom-left
        boxes.extend([
            (20.0, (100, 800, 150, 150)),
            (21.0, (110, 810, 150, 150)),
        ])

        cluster = cluster_face_boxes(boxes, frame_width=1920, frame_height=1080)
        self.assertIsNotNone(cluster)
        self.assertEqual(cluster["count"], 10)
        self.assertEqual(cluster["corner"], "top_right")
        med_x, med_y, med_w, med_h = cluster["median_box"]
        self.assertTrue(1390 <= med_x <= 1430)
        self.assertTrue(80 <= med_y <= 110)

    def test_expand_box_with_padding(self):
        box = (1400, 100, 200, 200)
        expanded = expand_box_with_padding(
            box, frame_width=1920, frame_height=1080, padding=0.25, target_aspect=1080.0 / 640.0
        )
        x, y, w, h = expanded
        # Must be within bounds
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, 1920)
        self.assertLessEqual(y + h, 1080)
        # Should be larger than original box
        self.assertGreater(w, 200)
        self.assertGreater(h, 200)

    def test_compute_gameplay_crop(self):
        # Top-right face -> gameplay should bias left
        gx, gy, gw, gh = compute_gameplay_crop(1920, 1080, "top_right")
        self.assertEqual(gy, 0)
        self.assertEqual(gh, 1080)
        self.assertLess(gx, 500)  # Biased left

        # Top-left face -> gameplay should bias right
        gx_l, gy_l, gw_l, gh_l = compute_gameplay_crop(1920, 1080, "top_left")
        self.assertGreater(gx_l, 500)  # Biased right

    @patch("clipper.facecam.sample_frames_from_video")
    def test_detect_facecam_not_found(self, mock_sample):
        # Video with 10 sampled frames, no faces detected
        dummy = np.zeros((1080, 1920, 3), dtype=np.uint8)
        mock_sample.return_value = [(float(i), dummy) for i in range(10)]

        res = detect_facecam("dummy.mp4")
        self.assertFalse(res["found"])
        self.assertEqual(res["layout"], "gameplay")
        self.assertEqual(res["presence_ratio"], 0.0)

    def test_vertical_render_layouts(self):
        """Test FFmpeg rendering for all 3 layouts (gameplay, split, face)."""
        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
        with tempfile.TemporaryDirectory() as temp_dir:
            rough_clip = os.path.join(temp_dir, "rough.mp4")
            # Generate a 1-second 1920x1080 test video
            cmd_gen = [
                ffmpeg_bin, "-y",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=1920x1080:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-c:a", "aac",
                rough_clip
            ]
            subprocess.run(cmd_gen, capture_output=True, check=True)

            mock_facecam = {
                "found": True,
                "layout": "split",
                "presence_ratio": 0.9,
                "face_box": (1400, 100, 250, 250),
                "padded_box": (1300, 50, 450, 300),
                "corner": "top_right",
                "frame_size": (1920, 1080),
            }

            # 1. Render 'gameplay' layout
            out_gameplay = os.path.join(temp_dir, "out_gameplay.mp4")
            render_vertical_clip(
                rough_clip,
                ass_path=None,
                output_path=out_gameplay,
                use_cpu=True,
                layout="gameplay",
                facecam_info=mock_facecam,
            )
            self.assertTrue(os.path.isfile(out_gameplay))
            self.assertGreater(os.path.getsize(out_gameplay), 5000)

            # 2. Render 'split' layout
            out_split = os.path.join(temp_dir, "out_split.mp4")
            render_vertical_clip(
                rough_clip,
                ass_path=None,
                output_path=out_split,
                use_cpu=True,
                layout="split",
                facecam_info=mock_facecam,
            )
            self.assertTrue(os.path.isfile(out_split))
            self.assertGreater(os.path.getsize(out_split), 5000)

            # 3. Render 'face' layout
            out_face = os.path.join(temp_dir, "out_face.mp4")
            render_vertical_clip(
                rough_clip,
                ass_path=None,
                output_path=out_face,
                use_cpu=True,
                layout="face",
                facecam_info=mock_facecam,
            )
            self.assertTrue(os.path.isfile(out_face))
            self.assertGreater(os.path.getsize(out_face), 5000)


if __name__ == "__main__":
    unittest.main()
