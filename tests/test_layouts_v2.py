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
    cam_region,
    cam_tile_geometry,
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

    def test_cam_shown_whole_with_capped_zoom(self):
        # a 465x261 cam in a 540-wide cell: shown whole (not a narrow slice stretched 3x)
        tw, th, sc = cam_tile_geometry((1452, 0, 465, 261), 540, 700, 1.5)
        self.assertEqual((tw, th), (540, 302))
        self.assertLessEqual(sc, 1.5)
        # a small cam is never enlarged past the cap, even if the cell is much wider
        tw, th, sc = cam_tile_geometry((0, 0, 300, 200), 1080, 800, 1.5)
        self.assertLessEqual(tw, 452)
        self.assertAlmostEqual(tw / th, 300 / 200, delta=0.02)

    def test_green_screen_region_is_generous(self):
        info = {"face_box": (100, 80, 90, 90)}
        x, y, w, h = cam_region(info, None, 1920, 1080)
        self.assertGreaterEqual(w, 300)          # head and shoulders, not a face crop
        self.assertTrue(x <= 100 and x + w >= 190 and y <= 80 and y + h >= 170)
        self.assertEqual(cam_region(None, {"rect": (10, 20, 400, 300)}, 1920, 1080), (10, 20, 400, 300))

    def test_collapse_repeats(self):
        words = [{"word": w} for w in ("I know what it is. " * 8 + "oh oh oh oh oh oh okay").split()]
        out = " ".join(w["word"] for w in collapse_repeats(words))
        self.assertEqual(out.count("know"), 3)
        self.assertEqual(out.split().count("oh"), 3)
        self.assertTrue(out.endswith("okay"))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- captions at the seams + emoji
def test_caption_anchor_sits_on_seam_or_below_middle():
    from clipper.cutter import TOP_BAND, caption_anchor_y, plan_cam_band
    info = {"frame_size": (1920, 1080), "cams": [{"rect": (1437, 0, 480, 321)}]}
    cam_h = plan_cam_band("split", info)
    assert 360 <= cam_h <= 800
    assert caption_anchor_y("split", info) == TOP_BAND + cam_h
    two = {"frame_size": (1920, 1080), "cams": [{"rect": (0, 0, 480, 321)}, {"rect": (1437, 0, 480, 321)}]}
    assert caption_anchor_y("duo", two) == TOP_BAND + plan_cam_band("duo", two)
    # a single picture: a bit below the middle of the 1920px frame
    assert 960 < caption_anchor_y("gameplay", None) < 1200


def test_make_ass_places_captions_at_given_y_and_plans_emoji(tmp_path):
    import json
    from clipper.captions import make_ass
    words = [(0.2 + i * 0.3, 0.45 + i * 0.3, w) for i, w in enumerate("HAHAHA THAT IS FUNNY MONEY IS HERE".split())]
    ass = str(tmp_path / "c.ass")
    make_ass(words, ass, layout="split", caption_y=900)
    text = open(ass, encoding="utf-8-sig").read()
    assert r"\an5\pos(540,900)" in text
    events = json.load(open(str(tmp_path / "c.emoji.json")))
    assert events and all(e["path"].endswith(".png") for e in events)
    # an old plan never leaks into a clip without emoji matches
    make_ass([(0.0, 0.3, "THE"), (0.3, 0.6, "TABLE")], ass, layout="split", caption_y=900)
    assert json.load(open(str(tmp_path / "c.emoji.json"))) == []


def test_emoji_planner_spaces_out_and_does_not_repeat_soon():
    from clipper.emoji import plan_emoji_events
    cards = [[(i * 0.5, i * 0.5 + 0.4, "money")] for i in range(30)]
    ev = plan_emoji_events(cards)
    assert 1 <= len(ev) <= 40
    starts = [e[0] for e in ev]
    assert all(b - a >= 4.0 - 1e-6 for a, b in zip(starts, starts[1:]))   # same emoji: >= 4 s apart
    mixed = [[(i * 1.0, i * 1.0 + 0.6, w)] for i, w in enumerate(["funny", "money", "cops", "table", "dead", "car"])]
    ev2 = plan_emoji_events(mixed)
    assert [e[2] for e in ev2] == ["😂", "💰", "🚓", "💀", "🚗"]


def test_emoji_vocabulary_is_broad_and_phrases_work():
    from clipper.emoji import pick_emoji_for_words
    assert pick_emoji_for_words("oh my god this is insane".split()) == "😱"
    assert pick_emoji_for_words("he was running away".split()) == "🏃"
    assert pick_emoji_for_words("i love this car".split()) in ("❤️", "🚗")
    assert pick_emoji_for_words(["table"]) is None


def test_known_cams_are_taken_back_when_a_face_is_there():
    from clipper.facecam import adopt_known_cams, merge_known_cams
    box = {"rect": (1437, 0, 480, 321), "face_box": (1578, 100, 94, 94), "kind": "box"}
    cut = {"rect": (0, 544, 624, 534), "face_box": (200, 600, 150, 150), "kind": "cutout"}
    # this clip found only the box cam
    cams = [{"rect": (1440, 0, 477, 321), "face_box": (1578, 100, 94, 94), "presence": 1.0}]
    # Speed leaned in: a bigger face in his spot in 8 of 10 samples
    dets = [(float(t), (190 + t % 2, 560, 190, 190)) for t in range(8)]
    out = adopt_known_cams(cams, [box, cut], dets, 1920, 1080, 10)
    assert len(out) == 2 and out[1].get("from_stream") and out[1]["kind"] == "cutout"
    # no face there -> nothing invented
    assert len(adopt_known_cams(cams, [box, cut], [], 1920, 1080, 10)) == 1
    # memory: the same overlay is not added twice
    known = merge_known_cams([], out)
    assert len(merge_known_cams(known, cams)) == len(known)



def test_clip_done_callback_errors_never_stop_rendering():
    from clipper.pipeline import _notify_clip_done
    got = []
    _notify_clip_done(got.append, {"path": "a.mp4"})
    assert got == [{"path": "a.mp4"}]
    _notify_clip_done(lambda c: 1 / 0, {"path": "b.mp4"})   # must not raise
    _notify_clip_done(None, {"path": "c.mp4"})


def test_ollama_payload_turns_thinking_off_and_keeps_model_loaded(monkeypatch):
    import json as _json
    from clipper import judge
    seen = {}

    class R:
        status = 200
        def read(self): return _json.dumps({"response": "{}"}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=0):
        seen["body"] = _json.loads(req.data.decode())
        return R()
    monkeypatch.setattr(judge.urllib.request, "urlopen", fake_open)
    assert judge.call_ollama_generate("hi", model="qwen3:14b") == "{}"
    b = seen["body"]
    assert b["think"] is False and b["format"] == "json" and b["keep_alive"] != 0
    assert b["options"]["num_ctx"] >= 4096


def test_ensure_ollama_never_raises_without_install(monkeypatch):
    from clipper import judge
    monkeypatch.setattr(judge, "is_ollama_available", lambda *a, **k: (False, []))
    monkeypatch.setattr(judge, "find_ollama_exe", lambda: None)
    assert judge.ensure_ollama_running("http://localhost:1", wait_seconds=0.1) is False
