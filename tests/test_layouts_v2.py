"""Cam panels, two-cam (duo) layout choice, crops, and Whisper loop clean-up."""
import os
import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clipper.facecam import (  # noqa: E402
    edge_persistence,
    find_panel_around,
    fit_crop_in_panel,
    gameplay_crop_avoiding,
    resolve_layout,
)
from clipper.transcription import collapse_repeats  # noqa: E402


def _overlap(a, b):
    return max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * \
        max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))


class TestLayoutsV2(unittest.TestCase):
    def test_panel_outline_found_on_moving_game(self):
        rng = np.random.default_rng(0)
        frames = []
        # still cam picture: a plain room with a couple of shelves
        cam = np.full((90, 150, 3), 90, np.uint8)
        cam[30:33, 10:80] = 200
        cam[60:90, 60:100] = 150
        for _ in range(10):
            # game: smooth shapes that change every frame
            f = cv2.GaussianBlur((rng.random((360, 640, 3)) * 255).astype(np.uint8), (0, 0), 6)
            f = cv2.normalize(f, None, 0, 255, cv2.NORM_MINMAX)
            f[:90, 490:640] = cam
            f[90:92, 488:640] = 255          # cam border (bottom)
            f[:92, 488:490] = 255            # cam border (left)
            frames.append(f)
        P, s = edge_persistence(frames)
        res = find_panel_around(P, (540, 30, 20, 20))
        self.assertIsNotNone(res)
        L, T, R, B = res["rect"]
        self.assertLess(abs(L - 488), 6)
        self.assertLess(abs(B - 91), 6)

    def test_resolve_layout(self):
        duo = {"cams": [{"rect": (0, 0, 300, 200)}, {"rect": (1600, 0, 300, 200)}], "found": True,
               "stream_type": "gaming_duo", "layout": "duo"}
        none = {"cams": [], "found": False, "stream_type": "irl", "layout": "gameplay", "people": 2}
        one = {"cams": [{"rect": (0, 0, 300, 200)}], "found": True, "stream_type": "gaming_facecam"}
        self.assertEqual(resolve_layout("split", duo)[0], "duo")
        self.assertEqual(resolve_layout("split", one)[0], "split")
        self.assertEqual(resolve_layout("split", none)[0], "gameplay")      # no cam here: Framed
        self.assertEqual(resolve_layout("split", none, explicit=True)[0], "split")
        self.assertEqual(resolve_layout("irl", none)[0], "gameplay")        # 2 people: Framed
        self.assertEqual(resolve_layout("irl", none, explicit=True)[0], "irl")
        self.assertEqual(resolve_layout("auto", duo)[0], "duo")

    def test_game_crop_avoids_both_cams(self):
        cams = [(0, 0, 465, 261), (1452, 0, 465, 279)]
        g = gameplay_crop_avoiding(1920, 1080, cams, 1080 / 768)
        self.assertTrue(all(_overlap(g, c) == 0 for c in cams))
        self.assertAlmostEqual(g[2] / g[3], 1080 / 768, delta=0.02)
        self.assertGreater(g[2], 900)

    def test_cam_crop_stays_inside_panel(self):
        panel = (1452, 0, 465, 279)
        c = fit_crop_in_panel(panel, (1600, 60, 90, 90), 540 / 768)
        self.assertGreaterEqual(c[0], panel[0])
        self.assertLessEqual(c[0] + c[2], panel[0] + panel[2])
        self.assertLessEqual(c[1] + c[3], panel[1] + panel[3])
        self.assertAlmostEqual(c[2] / c[3], 540 / 768, delta=0.02)

    def test_collapse_repeats(self):
        words = [{"word": w} for w in ("I know what it is. " * 8 + "oh oh oh oh oh oh okay").split()]
        out = " ".join(w["word"] for w in collapse_repeats(words))
        self.assertEqual(out.count("know"), 3)
        self.assertEqual(out.split().count("oh"), 3)
        self.assertTrue(out.endswith("okay"))


if __name__ == "__main__":
    unittest.main()
