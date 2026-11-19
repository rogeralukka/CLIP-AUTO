"""Facecam detection and auto-layout module for vertical 9:16 short clips.

Detects streamer facecam across sampled frames and determines optimal vertical layout:
- 'split': Facecam at top (1080x640), gameplay at bottom (1080x1280)
- 'gameplay': 16:9 gameplay fit in center with blurred/darkened background fill
- 'face': Reaction crop centered on streamer facecam scaled to 9:16
"""
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from .config import load_config

logger = logging.getLogger("clipper.facecam")

# Base directory for built-in models
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(BASE_DIR, "models")
BLAZE_FACE_PATH = os.path.join(MODELS_DIR, "blaze_face_short_range.tflite")
HAAR_CASCADE_PATH = os.path.join(MODELS_DIR, "haarcascade_frontalface_default.xml")


class FaceDetectorWrapper:
    """Wrapper supporting MediaPipe FaceDetector with graceful fallback to OpenCV Haar Cascade."""

    def __init__(self):
        self._mp_detector = None
        self._haar_cascade = None
        self._backend = "none"

        # Try MediaPipe first
        if os.path.isfile(BLAZE_FACE_PATH):
            try:
                import mediapipe as mp
                from mediapipe.tasks import python as mp_python
                from mediapipe.tasks.python import vision

                base_options = mp_python.BaseOptions(model_asset_path=BLAZE_FACE_PATH)
                options = vision.FaceDetectorOptions(
                    base_options=base_options, min_detection_confidence=0.5
                )
                self._mp_detector = vision.FaceDetector.create_from_options(options)
                self._backend = "mediapipe"
                logger.debug("Initialized MediaPipe FaceDetector backend.")
            except Exception as e:
                logger.warning(f"Failed to initialize MediaPipe FaceDetector: {e}")

        # Fallback to Haar Cascade
        if self._backend == "none" and os.path.isfile(HAAR_CASCADE_PATH):
            try:
                cascade = cv2.CascadeClassifier(HAAR_CASCADE_PATH)
                if not cascade.empty():
                    self._haar_cascade = cascade
                    self._backend = "haar"
                    logger.debug("Initialized OpenCV Haar Cascade backend.")
            except Exception as e:
                logger.warning(f"Failed to initialize OpenCV Haar Cascade: {e}")

    @property
    def backend(self) -> str:
        return self._backend

    def detect_faces(self, frame_bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
        """Detect faces on one image (multi-scale tiling lives in detect_faces_multiscale)."""
        return self._detect_raw(frame_bgr)

    def _detect_raw(self, frame_bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
        """Run the active backend on one image. Returns list of (x, y, w, h) bounding boxes."""
        h, w = frame_bgr.shape[:2]
        boxes: List[Tuple[int, int, int, int]] = []

        if self._backend == "mediapipe" and self._mp_detector is not None:
            try:
                import mediapipe as mp
                rgb = np.ascontiguousarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                results = self._mp_detector.detect(mp_img)
                for det in results.detections:
                    bb = det.bounding_box
                    # Clamp to frame bounds
                    bx = max(0, int(bb.origin_x))
                    by = max(0, int(bb.origin_y))
                    bw = min(w - bx, int(bb.width))
                    bh = min(h - by, int(bb.height))
                    if bw > 10 and bh > 10:
                        boxes.append((bx, by, bw, bh))
                return boxes
            except Exception as e:
                logger.debug(f"MediaPipe detection error: {e}")

        if self._backend == "haar" and self._haar_cascade is not None:
            try:
                gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
                detections = self._haar_cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30)
                )
                for (x, y, bw, bh) in detections:
                    boxes.append((int(x), int(y), int(bw), int(bh)))
                return boxes
            except Exception as e:
                logger.debug(f"Haar detection error: {e}")

        return boxes


# Singleton detector instance
_DETECTOR: Optional[FaceDetectorWrapper] = None


def detect_faces_multiscale(detector: "FaceDetectorWrapper", frame_bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Detect faces on the full frame AND on 2x-upscaled overlapping tiles.

    A streamer's facecam is small in a 1080p frame (face ~80-150px), below what the
    short-range face model reliably finds. Zooming into tiles makes those faces big enough.
    """
    h, w = frame_bgr.shape[:2]
    found = list(detector.detect_faces(frame_bgr))
    tw, th = w // 2, h // 2
    for ty in (0, h // 4, h - th):
        for tx in (0, w // 4, w - tw):
            tile = frame_bgr[ty:ty + th, tx:tx + tw]
            if tile.size == 0:
                continue
            big = cv2.resize(tile, (tw * 2, th * 2), interpolation=cv2.INTER_LINEAR)
            for (x, y, bw, bh) in detector.detect_faces(big):
                found.append((tx + x // 2, ty + y // 2, max(1, bw // 2), max(1, bh // 2)))
    # Drop near-duplicates (same face found on several tiles)
    unique: List[Tuple[int, int, int, int]] = []
    for b in sorted(found, key=lambda r: -r[2] * r[3]):
        cx, cy = b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
        if not any(abs(cx - (u[0] + u[2] / 2.0)) < u[2] * 0.6 and abs(cy - (u[1] + u[3] / 2.0)) < u[3] * 0.6
                   for u in unique):
            unique.append(b)
    return unique


def fine_face_pass(detector: "FaceDetectorWrapper", samples: List[Tuple[float, np.ndarray]],
                   max_frames: int = 8) -> List[Tuple[float, Tuple[int, int, int, int]]]:
    """Find SMALL faces: a streamer's facecam in a 1080p frame.

    The face model looks at a ~128px picture, so a 70px face in a 1920px frame is ~5px to it and
    is missed (even in 2x tiles). Here a few frames are scanned in many small overlapping tiles
    (the face fills a good part of each), faces that keep showing up in the same place become
    candidate spots, and each spot is then checked on EVERY sampled frame by looking only at a
    small crop around it.
    """
    n = len(samples)
    if n == 0:
        return []
    idx = sorted({int(i) for i in np.linspace(0, n - 1, min(max_frames, n))})
    h, w = samples[0][1].shape[:2]
    sizes = sorted({max(160, int(h * 0.45)), max(120, int(h * 0.28))}, reverse=True)
    raw: List[Tuple[int, int, int, int, int]] = []
    for i in idx:
        frame = samples[i][1]
        for ts in sizes:
            step = max(1, ts // 2)
            ys = list(range(0, max(1, h - ts + 1), step))
            xs = list(range(0, max(1, w - ts + 1), step))
            if ys[-1] != max(0, h - ts):
                ys.append(max(0, h - ts))
            if xs[-1] != max(0, w - ts):
                xs.append(max(0, w - ts))
            for ty in ys:
                for tx in xs:
                    tile = frame[ty:ty + ts, tx:tx + ts]
                    if tile.size == 0:
                        continue
                    for (x, y, bw, bh) in detector.detect_faces(tile):
                        if bh >= 0.03 * h:
                            raw.append((tx + x, ty + y, bw, bh, i))
    # group the finds that sit in the same place
    groups: List[Dict[str, Any]] = []
    for (x, y, bw, bh, i) in sorted(raw, key=lambda r: -r[2] * r[3]):
        cx, cy = x + bw / 2.0, y + bh / 2.0
        for g in groups:
            gx, gy, gw, gh = g["box"]
            if abs(cx - (gx + gw / 2.0)) < gw * 0.6 and abs(cy - (gy + gh / 2.0)) < gh * 0.6:
                g["frames"].add(i)
                g["boxes"].append((x, y, bw, bh))
                break
        else:
            groups.append({"box": (x, y, bw, bh), "frames": {i}, "boxes": [(x, y, bw, bh)]})
    need = max(2, int(np.ceil(0.3 * len(idx))))
    spots = [g for g in groups if len(g["frames"]) >= need]
    out: List[Tuple[float, Tuple[int, int, int, int]]] = []
    for g in spots:
        bx = np.median(np.array(g["boxes"], dtype=np.float32), axis=0)
        gx, gy, gw, gh = [float(v) for v in bx]
        side = int(max(gw, gh) * 3.0)
        x0 = int(max(0, min(w - side, gx + gw / 2.0 - side / 2.0)))
        y0 = int(max(0, min(h - side, gy + gh / 2.0 - side / 2.0)))
        side = max(1, min(side, w - x0, h - y0))
        for t, frame in samples:
            crop = frame[y0:y0 + side, x0:x0 + side]
            if crop.size == 0:
                continue
            best = None
            for (x, y, bw, bh) in detector.detect_faces(crop):
                # same spot and about the same size as the spot's face
                if abs((x + bw / 2.0) - (gx + gw / 2.0 - x0)) < gw * 0.7 and abs((y + bh / 2.0) - (gy + gh / 2.0 - y0)) < gh * 0.7 \
                        and 0.5 * gh <= bh <= 1.8 * gh:
                    if best is None or bw * bh > best[2] * best[3]:
                        best = (x + x0, y + y0, bw, bh)
            if best is not None:
                out.append((t, best))
    return out


def _add_fine_detections(all_detections, fine) -> int:
    """Add small-face finds to the list unless the same face is already there. Returns how many were added."""
    added = 0
    for t, b in fine:
        cx, cy = b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
        if any(abs(t0 - t) < 1e-6 and abs(cx - (b0[0] + b0[2] / 2.0)) < b[2] * 0.6 and abs(cy - (b0[1] + b0[3] / 2.0)) < b[3] * 0.6
               for t0, b0 in all_detections):
            continue
        all_detections.append((t, b))
        added += 1
    return added


def get_face_detector() -> FaceDetectorWrapper:
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = FaceDetectorWrapper()
    return _DETECTOR


def sample_frames_from_video(
    video_path: str,
    start_sec: float = 0.0,
    end_sec: Optional[float] = None,
    interval_sec: float = 1.0,
) -> List[Tuple[float, np.ndarray]]:
    """Sample frames from a video file at regular time intervals.

    Returns a list of (timestamp_sec, frame_bgr) tuples.
    """
    if not os.path.isfile(video_path):
        logger.warning(f"Video file not found: {video_path}")
        return []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.warning(f"Could not open video: {video_path}")
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0 or np.isnan(fps):
        fps = 30.0

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    video_duration = total_frames / fps if total_frames > 0 else 0.0

    actual_start = max(0.0, start_sec)
    actual_end = end_sec if (end_sec is not None and end_sec > actual_start) else (video_duration or 60.0)

    sampled: List[Tuple[float, np.ndarray]] = []
    current_t = actual_start

    try:
        while current_t <= actual_end:
            cap.set(cv2.CAP_PROP_POS_MSEC, current_t * 1000.0)
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            sampled.append((current_t, frame))
            current_t += interval_sec
    finally:
        cap.release()

    return sampled


def cluster_face_boxes(
    boxes_with_times: List[Tuple[float, Tuple[int, int, int, int]]],
    frame_width: int,
    frame_height: int,
    drift_tolerance: float = 0.15,
) -> Optional[Dict[str, Any]]:
    """Find the dominant stable face bounding box cluster across sampled frames.

    Args:
        boxes_with_times: List of (timestamp, (x, y, w, h))
        frame_width: Width of video
        frame_height: Height of video
        drift_tolerance: Allowed relative drift in coordinates (default 15%)

    Returns:
        Dict with cluster metadata or None if no consistent cluster.
    """
    if not boxes_with_times:
        return None

    # Group boxes that are close to each other
    clusters: List[List[Tuple[float, Tuple[int, int, int, int]]]] = []
    max_dist_x = frame_width * drift_tolerance
    max_dist_y = frame_height * drift_tolerance

    for t, box in boxes_with_times:
        bx, by, bw, bh = box
        cx = bx + bw / 2.0
        cy = by + bh / 2.0

        matched = False
        for c in clusters:
            # Average center of cluster
            avg_cx = np.mean([b[1][0] + b[1][2] / 2.0 for b in c])
            avg_cy = np.mean([b[1][1] + b[1][3] / 2.0 for b in c])
            if abs(cx - avg_cx) <= max_dist_x and abs(cy - avg_cy) <= max_dist_y:
                c.append((t, box))
                matched = True
                break

        if not matched:
            clusters.append([(t, box)])

    if not clusters:
        return None

    # Pick the cluster seen in the most distinct frames (a facecam stays put; faces of
    # in-game characters wander and never build a big cluster).
    clusters.sort(key=lambda c: len({round(b[0], 2) for b in c}), reverse=True)
    best_cluster = clusters[0]

    # Compute median bounding box
    xs = [b[1][0] for b in best_cluster]
    ys = [b[1][1] for b in best_cluster]
    ws = [b[1][2] for b in best_cluster]
    hs = [b[1][3] for b in best_cluster]

    med_x = int(np.median(xs))
    med_y = int(np.median(ys))
    med_w = int(np.median(ws))
    med_h = int(np.median(hs))

    # Identify corner
    center_x = med_x + med_w / 2.0
    center_y = med_y + med_h / 2.0
    corner_x = "left" if center_x < frame_width / 2.0 else "right"
    corner_y = "top" if center_y < frame_height / 2.0 else "bottom"
    corner = f"{corner_y}_{corner_x}"

    return {
        "count": len({round(b[0], 2) for b in best_cluster}),
        "median_box": (med_x, med_y, med_w, med_h),
        "corner": corner,
        "boxes": best_cluster,
    }


def expand_box_with_padding(
    box: Tuple[int, int, int, int],
    frame_width: int,
    frame_height: int,
    padding: float = 0.25,
    target_aspect: float = 1080.0 / 640.0,  # 1.6875 aspect ratio for 1080x640 split header
) -> Tuple[int, int, int, int]:
    """Expand a face bounding box with natural padding and enforce target aspect ratio.

    Clamps strictly within [0, frame_width] and [0, frame_height].
    """
    x, y, w, h = box
    pad_w = int(w * padding)
    pad_h = int(h * padding)

    # Initial padded box. More room below the face than above (shoulders, not ceiling).
    x0 = max(0, x - pad_w)
    y0 = max(0, y - int(pad_h * 0.7))
    x1 = min(frame_width, x + w + pad_w)
    y1 = min(frame_height, y + h + int(pad_h * 1.3))

    curr_w = x1 - x0
    curr_h = y1 - y0

    # Adjust to target_aspect (w / h = target_aspect)
    # If wider than aspect, expand height; if taller, expand width
    desired_w = curr_w
    desired_h = curr_h
    if curr_w / max(1, curr_h) < target_aspect:
        # Too tall: expand width
        desired_w = int(curr_h * target_aspect)
    else:
        # Too wide: expand height
        desired_h = int(curr_w / target_aspect)

    # Re-center expanded box on original face center
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2

    new_x0 = max(0, cx - desired_w // 2)
    new_y0 = max(0, cy - desired_h // 2)
    new_x1 = min(frame_width, new_x0 + desired_w)
    new_y1 = min(frame_height, new_y0 + desired_h)

    # Shift back if touching right/bottom edge
    if new_x1 - new_x0 < desired_w and new_x0 > 0:
        new_x0 = max(0, new_x1 - desired_w)
    if new_y1 - new_y0 < desired_h and new_y0 > 0:
        new_y0 = max(0, new_y1 - desired_h)

    final_w = new_x1 - new_x0
    final_h = new_y1 - new_y0

    return (new_x0, new_y0, max(10, final_w), max(10, final_h))


def detect_facecam(
    video_path: str,
    start_sec: float = 0.0,
    end_sec: Optional[float] = None,
    sample_interval: Optional[float] = None,
    presence_threshold: Optional[float] = None,
    box_padding: Optional[float] = None,
    known_cams: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Detect streamer facecam across video duration and select layout.

    `known_cams` are cams already found in other parts of the SAME stream. An overlay stays in
    place for the whole stream, so a cam that is missed here (the streamer leaned in, a
    different scale) is taken back from it when a face really is in that spot.

    Returns dict:
    {
        "found": bool,
        "layout": "split" | "gameplay",
        "presence_ratio": float,
        "face_box": (x, y, w, h) or None,
        "padded_box": (x, y, w, h) or None,
        "corner": str,
        "frame_size": (width, height),
        "total_samples": int,
        "detected_samples": int
    }
    """
    cfg = load_config()
    fc_cfg = cfg.get("facecam", {})
    interval = sample_interval or fc_cfg.get("sample_interval_seconds", 1.0)
    thresh = presence_threshold or fc_cfg.get("presence_threshold", 0.60)
    padding = box_padding or fc_cfg.get("box_padding", 0.25)

    samples = sample_frames_from_video(video_path, start_sec, end_sec, interval)
    if not samples:
        return {
            "found": False,
            "layout": "gameplay",
            "presence_ratio": 0.0,
            "face_box": None,
            "padded_box": None,
            "corner": "unknown",
            "frame_size": (1920, 1080),
            "total_samples": 0,
            "detected_samples": 0,
        }

    detector = get_face_detector()
    frame_h, frame_w = samples[0][1].shape[:2]

    all_detections: List[Tuple[float, Tuple[int, int, int, int]]] = []
    frames_with_face = 0

    for t, frame in samples:
        boxes = detect_faces_multiscale(detector, frame)
        if boxes:
            frames_with_face += 1
            for b in boxes:
                all_detections.append((t, b))

    # Small faces (a facecam in a 1080p frame) are invisible to the whole-frame pass
    try:
        if _add_fine_detections(all_detections, fine_face_pass(detector, samples)):
            frames_with_face = len({round(t, 2) for t, _ in all_detections})
    except Exception as e:
        logger.warning(f"Small-face pass failed: {e}")

    total_samples = len(samples)
    cluster = cluster_face_boxes(all_detections, frame_w, frame_h)

    if cluster is None:
        return {
            "found": False,
            "layout": "gameplay",
            "stream_type": "gaming",
            "track": [],
            "presence_ratio": 0.0,
            "face_box": None,
            "padded_box": None,
            "corner": "unknown",
            "frame_size": (frame_w, frame_h),
            "total_samples": total_samples,
            "detected_samples": 0,
        }

    # The biggest face cluster is not always the cam: a game character's face can be bigger or
    # show up more. Among the clusters that stay put, prefer one that looks like a cam (still,
    # near an edge, small, there most of the time) - the stillest one wins.
    cam_pick = None
    try:
        big_ratio = float(fc_cfg.get("fullscreen_face_ratio", 0.22))
        max_drift = float(fc_cfg.get("facecam_max_drift", 0.03))
        best_key = None
        for c in _stable_clusters(all_detections, frame_w, frame_h, total_samples):
            mb = c["median_box"]
            pres = min(c["count"], total_samples) / max(1, total_samples)
            ccx = np.array([q[1][0] + q[1][2] / 2.0 for q in c["boxes"]], dtype=np.float32)
            ccy = np.array([q[1][1] + q[1][3] / 2.0 for q in c["boxes"]], dtype=np.float32)
            dr = float(max(np.std(ccx) / frame_w, np.std(ccy) / frame_h)) if len(ccx) > 1 else 0.0
            mx, my = (mb[0] + mb[2] / 2.0) / frame_w, (mb[1] + mb[3] / 2.0) / frame_h
            edge = mx < 0.35 or mx > 0.65 or my < 0.30 or my > 0.70
            if pres >= thresh and mb[3] < frame_h * big_ratio and edge and dr <= max_drift:
                key = (dr, -pres)
                if best_key is None or key < best_key:
                    best_key, cam_pick = key, c
        if cam_pick is not None:
            cluster = cam_pick
    except Exception as e:
        logger.warning(f"Cam-like face search failed: {e}")

    cluster_count = min(cluster["count"], total_samples)
    presence_ratio = round(cluster_count / max(1, total_samples), 3)

    med_box = cluster["median_box"]
    padded_box = expand_box_with_padding(med_box, frame_w, frame_h, padding)

    # How still is the main face? A facecam overlay sits in one place; a person filmed by a
    # camera (IRL) moves around the frame.
    cxs = np.array([b[1][0] + b[1][2] / 2.0 for b in cluster["boxes"]], dtype=np.float32)
    cys = np.array([b[1][1] + b[1][3] / 2.0 for b in cluster["boxes"]], dtype=np.float32)
    drift = float(max(np.std(cxs) / frame_w, np.std(cys) / frame_h)) if len(cxs) > 1 else 0.0
    # Frames where faces show up OUTSIDE the main cluster (other people, moving camera)
    cluster_keys = {(round(t, 2), tuple(b)) for t, b in cluster["boxes"]}
    other_times = {round(t, 2) for t, b in all_detections if (round(t, 2), tuple(b)) not in cluster_keys}
    other_ratio = len(other_times) / max(1, total_samples)
    any_face_ratio = frames_with_face / max(1, total_samples)

    big_face_ratio = float(fc_cfg.get("fullscreen_face_ratio", 0.22))
    is_big = med_box[3] >= frame_h * big_face_ratio
    stable = drift <= float(fc_cfg.get("facecam_max_drift", 0.03))

    # Facecam overlays sit near an edge/corner of the screen, not in the middle of the scene
    mcx = (med_box[0] + med_box[2] / 2.0) / frame_w
    mcy = (med_box[1] + med_box[3] / 2.0) / frame_h
    near_edge = mcx < 0.35 or mcx > 0.65 or mcy < 0.30 or mcy > 0.70

    # Real cam rectangles (one per streamer: co-op streams show two cams)
    frames = [f for _, f in samples]
    seeds = [c["median_box"] for c in _stable_clusters(all_detections, frame_w, frame_h, total_samples)]
    try:
        cams = find_cam_panels(frames, seeds, detector)
    except Exception as e:  # never let the panel finder break a render
        logger.warning(f"Cam panel search failed: {e}")
        cams = []

    # Cams without a rectangle (green screen: the person is cut out over the game)
    try:
        cams = cams + find_cutout_cams(_stable_clusters(all_detections, frame_w, frame_h, total_samples),
                                       frames, cams, frame_w, frame_h, total_samples)
    except Exception as e:
        logger.warning(f"Cutout cam search failed: {e}")

    # Cams already known from other parts of this stream: one overlay for the whole stream
    if known_cams:
        try:
            cams = adopt_known_cams(cams, known_cams, all_detections, frame_w, frame_h, total_samples)
        except Exception as e:
            logger.warning(f"Using known cams failed: {e}")

    # How many people are on screen at once (big faces, outside any cam)
    def _in_cam(b):
        cx, cy = b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
        return any(r[0] <= cx <= r[0] + r[2] and r[1] <= cy <= r[1] + r[3] for r in (c["rect"] for c in cams))
    per_t: Dict[float, int] = {}
    for t, b in all_detections:
        if b[3] >= 0.10 * frame_h and not _in_cam(b):
            per_t[round(t, 2)] = per_t.get(round(t, 2), 0) + 1
    group_ratio = sum(1 for v in per_t.values() if v >= 2) / max(1, total_samples)
    people = 2 if group_ratio >= 0.3 else 1

    # One picture, no separate cam: default to "gameplay" (Framed: the whole 16:9 picture in
    # the middle over a blurred fill). Follow / Face stay available per clip.
    single = str(fc_cfg.get("single_source_layout", "gameplay"))

    # Truly distinct co-op duo cams: both must have high face presence (>= 0.50) in this clip
    # and both must be real boxed cams (kind == "box"), with physical separation on screen
    is_true_duo = (
        len(cams) >= 2
        and all(c.get("presence", 0) >= 0.50 for c in cams[:2])
        and all(c.get("kind") == "box" for c in cams[:2])
        and abs(cams[0]["rect"][0] - cams[1]["rect"][0]) > frame_w * 0.30
    )

    if presence_ratio >= thresh and is_big and stable:
        # One big face that stays put: Just Chatting / reaction cam filling the screen
        stream_type, layout, found = "just_chatting", single, True
    elif is_true_duo:
        # Two streamers' cams (duo / co-op stream): both cams on top, game below
        cams = sorted(cams[:2], key=lambda c: c["rect"][0])
        stream_type, layout, found = "gaming_duo", "duo", True
    elif len(cams) >= 1:
        # Solo gaming facecam: choose the single best cam
        best_cam = max(cams, key=lambda c: (c.get("kind") == "box", c.get("presence", 0.0), -c["rect"][2]))
        cams = [best_cam]
        stream_type, layout, found = "gaming_facecam", "split", True
    elif presence_ratio >= thresh and not is_big and near_edge and (
            (stable and other_ratio < 0.35) or drift <= 0.012 or cam_pick is not None):
        # Cam found by its face only (no clear outline). A very still face near the edge is
        # a cam even when the game shows faces of its own (story games).
        stream_type, layout, found = "gaming_facecam", "split", True
    elif any_face_ratio >= 0.30:
        # People filmed by a camera, moving around or several of them: IRL / event stream
        stream_type, layout, found = "irl", single, False
    else:
        stream_type, layout, found = "gaming", "gameplay", False

    if found and cams:
        presence_ratio = round(float(np.mean([c["presence"] for c in cams])), 3)
        med_box = cams[0]["face_box"]
        corner = cams[0]["corner"]
    else:
        corner = cluster["corner"]

    track = build_subject_track(all_detections, frame_w, frame_h) if stream_type in ("irl", "just_chatting") else []

    logger.info(
        f"Stream type: {stream_type} (layout={layout}; cams={[c['rect'] for c in cams]}, "
        f"face presence={presence_ratio:.0%}, drift={drift:.3f}, other faces in {other_ratio:.0%} of frames, "
        f"people={people})"
    )

    return {
        "found": found,
        "layout": layout,
        "stream_type": stream_type,
        "presence_ratio": presence_ratio,
        "face_box": med_box,
        "padded_box": padded_box,
        "cams": cams,
        "people": people,
        "corner": corner,
        "frame_size": (frame_w, frame_h),
        "total_samples": total_samples,
        "detected_samples": cluster_count,
        "drift": round(drift, 4),
        "track": track,
    }


def _stable_clusters(detections, frame_w: int, frame_h: int, total: int,
                     min_ratio: float = 0.25) -> List[Dict[str, Any]]:
    """All face clusters that stay put in a good share of frames (one per cam)."""
    remaining = list(detections)
    out = []
    for _ in range(4):
        c = cluster_face_boxes(remaining, frame_w, frame_h, drift_tolerance=0.06)
        if not c or c["count"] < max(2, min_ratio * total):
            break
        out.append(c)
        keys = {(round(t, 2), tuple(b)) for t, b in c["boxes"]}
        remaining = [(t, b) for t, b in remaining if (round(t, 2), tuple(b)) not in keys]
    return out


def resolve_layout(requested: Optional[str], info: Optional[Dict[str, Any]],
                   explicit: bool = False) -> Tuple[str, str]:
    """Turn the layout the user asked for into one that fits what is actually on screen.

    Returns (layout, note). "split" means "game + facecam": with two streamer cams it becomes
    "duo" (so nobody gets cut out); with no cam in this part of the stream it falls back to
    Framed instead of showing the game twice. `explicit` = the user picked this layout for
    this one clip (a layout button), so only the two-cam upgrade is applied.
    """
    info = info or {}
    req = (requested or "auto").lower()
    cams = info.get("cams") or []
    if req == "auto":
        return (info.get("layout") or "gameplay"), ""
    if req in ("split", "duo"):
        if len(cams) >= 2:
            return "duo", ("two cams found: both shown" if req == "split" else "")
        if info.get("found") and info.get("stream_type") in ("gaming_facecam", "gaming_duo"):
            return "split", ""
        if explicit:
            return "split", ""
        return "gameplay", "no facecam in this part of the stream, used Framed"
    if req in ("irl", "follow"):
        if info.get("people", 1) >= 2 and not explicit:
            return "gameplay", "several people on screen, used Framed so nobody is cut out"
        return "irl", ""
    return req, ""


def build_subject_track(
    detections: List[Tuple[float, Tuple[int, int, int, int]]],
    frame_w: int,
    frame_h: int,
) -> List[Tuple[float, float]]:
    """Per-second horizontal centre of the main person, smoothed so the crop glides.

    Picks the biggest face each sample (sticking with the previous subject when sizes are
    close), fills gaps, then smooths and limits how fast the crop may pan.
    """
    by_t: Dict[float, List[Tuple[int, int, int, int]]] = {}
    for t, b in detections:
        by_t.setdefault(round(t, 2), []).append(b)
    if not by_t:
        return []
    times = sorted(by_t)
    xs: List[float] = []
    prev_x: Optional[float] = None
    for t in times:
        boxes = by_t[t]
        def key(b):
            area = b[2] * b[3]
            near = 0.0 if prev_x is None else abs((b[0] + b[2] / 2.0) - prev_x) / frame_w
            return area * (1.0 - min(0.6, near))
        b = max(boxes, key=key)
        prev_x = b[0] + b[2] / 2.0
        # Several people close together (e.g. two people talking): frame them all when
        # they fit in the 9:16 window instead of following only the biggest face.
        crop_w = frame_h * 9.0 / 16.0
        people = [p for p in boxes if p[3] >= 0.45 * b[3]]
        if len(people) >= 2:
            lo = min(p[0] for p in people)
            hi = max(p[0] + p[2] for p in people)
            if hi - lo <= crop_w * 0.85:
                prev_x = (lo + hi) / 2.0
        xs.append(prev_x)
    arr = np.array(xs, dtype=np.float32)
    if len(arr) >= 3:
        k = min(5, len(arr) | 1)
        pad = k // 2
        arr = np.convolve(np.pad(arr, pad, mode="edge"), np.ones(k) / k, mode="valid")
    # Limit pan speed to ~15% of the frame width per second
    max_step = 0.15 * frame_w
    for i in range(1, len(arr)):
        dt = max(0.5, times[i] - times[i - 1])
        step = max_step * dt
        arr[i] = arr[i - 1] + float(np.clip(arr[i] - arr[i - 1], -step, step))
    return [(float(t), round(float(x), 1)) for t, x in zip(times, arr)]


def compute_gameplay_crop(
    frame_width: int,
    frame_height: int,
    face_corner: str,
    target_aspect: float = 1080.0 / 1280.0,  # 0.84375 for 1080x1280 bottom area
) -> Tuple[int, int, int, int]:
    """Compute crop for gameplay area excluding the corner containing facecam.

    If face is in 'top_right', bias gameplay crop towards the left/center.
    If face is in 'top_left', bias gameplay crop towards the right/center.
    """
    # Desired crop width and height at target_aspect
    crop_h = frame_height
    crop_w = int(crop_h * target_aspect)

    if crop_w > frame_width:
        crop_w = frame_width
        crop_h = int(crop_w / target_aspect)

    # Bias x based on face corner to avoid duplicating facecam
    # Push the crop as far from the cam as possible so the cam doesn't show twice
    if "right" in face_corner:
        x = 0
    elif "left" in face_corner:
        x = frame_width - crop_w
    else:
        # Center crop
        x = (frame_width - crop_w) // 2

    y = (frame_height - crop_h) // 2
    return (x, y, crop_w, crop_h)


# --------------------------------------------------------------------------- cam panels
# A facecam is a separate video pasted on top of the game as a rectangle. Its border
# stays in the same place in every frame while the game underneath keeps changing, so
# edges that are present in (almost) every sampled frame outline the cam exactly. That
# gives the real cam rectangle instead of "face box + guessed padding", which used to
# pull half the game into the cam band.

def edge_persistence(frames: List[np.ndarray], width: int = 640) -> Tuple[np.ndarray, float]:
    """Fraction of sampled frames in which each pixel is on an edge (at analysis width)."""
    if not frames:
        return np.zeros((1, 1), np.float32), 1.0
    h0, w0 = frames[0].shape[:2]
    scale = width / float(w0)
    size = (width, max(1, int(round(h0 * scale))))
    acc = None
    k = np.ones((3, 3), np.uint8)
    for f in frames:
        g = cv2.cvtColor(cv2.resize(f, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(g, (3, 3), 0)
        e = cv2.dilate(cv2.Canny(g, 40, 120), k) > 0
        acc = e.astype(np.float32) if acc is None else acc + e
    return acc / float(len(frames)), scale


def _col_cov(P: np.ndarray, x: int, y0: int, y1: int, thr: float = 0.6) -> float:
    W = P.shape[1]
    if y1 <= y0:
        return 0.0
    band = P[y0:y1, max(0, x - 1):min(W, x + 2)]
    return float((band.max(axis=1) >= thr).mean()) if band.size else 0.0


def _row_cov(P: np.ndarray, y: int, x0: int, x1: int, thr: float = 0.6) -> float:
    H = P.shape[0]
    if x1 <= x0:
        return 0.0
    band = P[max(0, y - 1):min(H, y + 2), x0:x1]
    return float((band.max(axis=0) >= thr).mean()) if band.size else 0.0


def find_panel_around(P: np.ndarray, seed: Tuple[int, int, int, int],
                      min_size: Tuple[float, float] = (1.5, 1.3),
                      max_area: float = 0.35) -> Optional[Dict[str, Any]]:
    """Smallest well-outlined rectangle around `seed` (x, y, w, h in P's pixels).

    Each side is either the frame edge or a line that is an edge in most frames along
    its whole length. Returns {"rect": (x0, y0, x1, y1), "real_sides": n, "cov": c}.
    """
    H, W = P.shape
    sx, sy, sw, sh = seed
    fx0, fy0, fx1, fy1 = max(0, sx), max(0, sy), min(W, sx + sw), min(H, sy + sh)
    if fx1 - fx0 < 2 or fy1 - fy0 < 2:
        return None
    edge = 3  # within this many px of the frame edge counts as "on the edge"

    def cands(lo, hi, cov_fn, border, rev):
        # every line that covers the seed's span; keep the strongest few (a real cam
        # border is an edge in ~every frame, busy game texture only in some)
        found = []
        for v in range(lo, hi + 1):
            c = cov_fn(v)
            if c >= 0.7:
                # equal strength: nearest the face first
                found.append((round(c, 2), -abs(v - (hi if rev else lo)), v))
        found.sort(reverse=True)
        out: List[int] = []
        for c, _, v in found:
            if all(abs(v - o) > 2 for o in out):
                out.append(v)
            if len(out) >= 12:
                break
        return out + [border]

    lefts = cands(edge, fx0 - 2, lambda x: _col_cov(P, x, fy0, fy1), 0, True)
    rights = cands(fx1 + 2, W - 1 - edge, lambda x: _col_cov(P, x, fy0, fy1), W - 1, False)
    tops = cands(edge, fy0 - 2, lambda y: _row_cov(P, y, fx0, fx1), 0, True)
    bots = cands(fy1 + 2, H - 1 - edge, lambda y: _row_cov(P, y, fx0, fx1), H - 1, False)

    _cc: Dict[Tuple[str, int, int, int], float] = {}

    def ccov(x, a, b):
        k = ("c", x, a, b)
        if k not in _cc:
            _cc[k] = _col_cov(P, x, a, b)
        return _cc[k]

    def rcov(y, a, b):
        k = ("r", y, a, b)
        if k not in _cc:
            _cc[k] = _row_cov(P, y, a, b)
        return _cc[k]

    best = None
    for L in lefts:
        for R in rights:
            w = R - L
            if w < min_size[0] * sw:
                continue
            for T in tops:
                for B in bots:
                    h = B - T
                    if h < min_size[1] * sh or w * h > max_area * W * H:
                        continue
                    if not (0.5 <= w / max(1, h) <= 2.9):
                        continue
                    covs = []
                    if L > edge:
                        covs.append(ccov(L, T, B))
                    if R < W - 1 - edge:
                        covs.append(ccov(R, T, B))
                    if T > edge:
                        covs.append(rcov(T, L, R))
                    if B < H - 1 - edge:
                        covs.append(rcov(B, L, R))
                    if len(covs) < 2 or min(covs) < 0.75:
                        continue
                    # The OUTER border wins: the cam's own background is static too, so
                    # lines inside the cam (shelves, desk) are also "persistent edges".
                    score = (w * h, float(np.mean(covs)))
                    if best is None or score > best[0]:
                        best = (score, (L, T, R, B), len(covs))
    if best is None:
        return None
    return {"rect": best[1], "real_sides": best[2], "cov": round(best[0][1], 3)}


def _faces_in_panel(detector: "FaceDetectorWrapper", frames: List[np.ndarray],
                    rect: Tuple[int, int, int, int]) -> Tuple[float, Optional[Tuple[int, int, int, int]]]:
    """Share of frames with a face inside the panel (panel zoomed up first, since cams are
    small), and the median face box in frame coordinates."""
    x, y, w, h = rect
    if w < 8 or h < 8:
        return 0.0, None
    zoom = max(1.0, min(4.0, 480.0 / h))
    hits, boxes = 0, []
    for f in frames:
        crop = f[y:y + h, x:x + w]
        if crop.size == 0:
            continue
        big = cv2.resize(crop, (int(w * zoom), int(h * zoom)), interpolation=cv2.INTER_CUBIC) if zoom > 1 else crop
        found = detector.detect_faces(big)
        # ignore tiny specks (faces in the background of the cam)
        found = [b for b in found if b[3] >= 0.12 * big.shape[0]]
        if found:
            hits += 1
            b = max(found, key=lambda r: r[2] * r[3])
            boxes.append((x + b[0] / zoom, y + b[1] / zoom, b[2] / zoom, b[3] / zoom))
    if not boxes:
        return hits / max(1, len(frames)), None
    med = tuple(int(np.median([b[i] for b in boxes])) for i in range(4))
    return hits / max(1, len(frames)), med  # type: ignore[return-value]


def find_cam_panels(frames: List[np.ndarray],
                    face_seeds: List[Tuple[int, int, int, int]],
                    detector: Optional["FaceDetectorWrapper"] = None,
                    min_presence: float = 0.3) -> List[Dict[str, Any]]:
    """Find facecam rectangles (frame pixel coords). Seeds: stable faces + the 4 corners."""
    if len(frames) < 3:
        return []
    detector = detector or get_face_detector()
    H0, W0 = frames[0].shape[:2]
    P, s = edge_persistence(frames)
    Hp, Wp = P.shape
    M = motion_map(frames, (Wp, Hp))

    seeds: List[Tuple[Tuple[int, int, int, int], Tuple[float, float]]] = []
    for (x, y, w, h) in face_seeds:
        seeds.append(((int(x * s), int(y * s), max(2, int(w * s)), max(2, int(h * s))), (1.5, 1.3)))
    # Corner / edge seeds find cams whose face the detector missed (dark, small, turned away)
    sw, sh = int(Wp * 0.05), int(Hp * 0.07)
    for fx in (0.06, 0.5 - 0.025, 0.89):
        for fy in (0.07, 0.86):
            seeds.append(((int(Wp * fx), int(Hp * fy), sw, sh), (2.2, 2.0)))

    panels: List[Dict[str, Any]] = []
    for seed, msz in seeds:
        res = find_panel_around(P, seed, min_size=msz)
        if not res:
            continue
        L, T, R, B = res["rect"]
        # A cam overlay hugs an edge of the screen
        gap = min(L / Wp, (Wp - 1 - R) / Wp, T / Hp, (Hp - 1 - B) / Hp)
        if gap > 0.06:
            continue
        rect = (int(L / s), int(T / s), int((R - L) / s), int((B - T) / s))
        if any(_iou(rect, p["rect"]) > 0.5 for p in panels):
            continue
        # A pasted-on cam has the game moving all around it. A rectangle that belongs to
        # the scene itself (a poster or TV behind an IRL streamer) has a still wall around
        # it while the person in front of it moves.
        ring, inside = _ring_motion(M, (L, T, R, B))
        if ring < 4.0 or ring < 1.15 * inside:
            continue
        presence, face = _faces_in_panel(detector, frames, rect)
        if presence < min_presence or face is None:
            continue
        cx = (rect[0] + rect[2] / 2.0) / W0
        cy = (rect[1] + rect[3] / 2.0) / H0
        panels.append({
            "rect": rect,
            "face_box": face,
            "presence": round(presence, 3),
            "real_sides": res["real_sides"],
            "edge_cov": res["cov"],
            "corner": f"{'top' if cy < 0.5 else 'bottom'}_{'left' if cx < 0.5 else 'right'}",
        })
    # Biggest, most reliable first
    panels.sort(key=lambda p: (p["presence"] >= 0.5, p["rect"][2] * p["rect"][3]), reverse=True)
    return panels


def motion_map(frames: List[np.ndarray], size: Tuple[int, int]) -> np.ndarray:
    """Mean absolute change between consecutive samples, per pixel (0-255 scale)."""
    g = [cv2.cvtColor(cv2.resize(f, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
         for f in frames]
    if len(g) < 2:
        return np.zeros((size[1], size[0]), np.float32)
    return np.mean([np.abs(a - b) for a, b in zip(g[1:], g[:-1])], axis=0)


def _ring_motion(M: np.ndarray, rect: Tuple[int, int, int, int]) -> Tuple[float, float]:
    L, T, R, B = rect
    H, W = M.shape
    r = max(3, int(W * 0.04))
    mask = np.zeros(M.shape, bool)
    mask[max(0, T - r):min(H, B + r), max(0, L - r):min(W, R + r)] = True
    mask[T:B, L:R] = False
    ring = float(M[mask].mean()) if mask.any() else 0.0
    inside = float(M[T:B, L:R].mean()) if (B > T and R > L) else 0.0
    return ring, inside


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def fit_crop_in_panel(panel: Tuple[int, int, int, int], face: Optional[Tuple[int, int, int, int]],
                      aspect: float) -> Tuple[int, int, int, int]:
    """Largest crop of the given aspect INSIDE the cam panel, centred on the face.
    Cropping inside (never padding outside) keeps the game out of the cam band."""
    x, y, w, h = panel
    if w / max(1, h) > aspect:
        cw, ch = int(h * aspect), h
    else:
        cw, ch = w, int(w / aspect)
    cw, ch = max(2, cw - cw % 2), max(2, ch - ch % 2)
    if face:
        fcx, fcy = face[0] + face[2] / 2.0, face[1] + face[3] / 2.0
    else:
        fcx, fcy = x + w / 2.0, y + h / 2.0
    # keep a little headroom: put the face slightly above centre
    cx0 = int(round(fcx - cw / 2.0))
    cy0 = int(round(fcy - ch * 0.45))
    cx0 = max(x, min(x + w - cw, cx0))
    cy0 = max(y, min(y + h - ch, cy0))
    return (cx0, cy0, cw, ch)


def gameplay_crop_avoiding(frame_w: int, frame_h: int, avoid: List[Tuple[int, int, int, int]],
                           aspect: float, max_overlap: float = 0.0) -> Tuple[int, int, int, int]:
    """Biggest crop of `aspect` that leaves out every cam panel (so no cam shows twice),
    nearest the centre of the screen when several fit."""
    best = None
    step = max(4, frame_h // 60)
    for ch in range(frame_h, int(frame_h * 0.45), -step):
        cw = int(ch * aspect)
        if cw > frame_w:
            cw = frame_w
            ch = int(cw / aspect)
        xs = sorted(set([0, frame_w - cw, (frame_w - cw) // 2] + list(range(0, frame_w - cw + 1, step))))
        ys = sorted(set([0, frame_h - ch, (frame_h - ch) // 2] + list(range(0, frame_h - ch + 1, step))))
        for y in ys:
            for x in xs:
                area = cw * ch
                ov = sum(max(0, min(x + cw, a[0] + a[2]) - max(x, a[0])) *
                         max(0, min(y + ch, a[1] + a[3]) - max(y, a[1])) for a in avoid)
                if ov > max_overlap * area:
                    continue
                d = (abs(x + cw / 2 - frame_w / 2) / frame_w) * 2.0 + abs(y + ch / 2 - frame_h / 2) / frame_h
                if best is None or d < best[0]:
                    best = (d, (x, y, cw, ch))
        if best is not None:
            x, y, cw, ch = best[1]
            return (x - x % 2, y - y % 2, cw - cw % 2, ch - ch % 2)
    # nothing avoids the cams: fall back to a centred crop
    cw = min(frame_w, int(frame_h * aspect))
    ch = min(frame_h, int(cw / aspect))
    return ((frame_w - cw) // 2, (frame_h - ch) // 2, cw, ch)


# --------------------------------------------------------------------------- cam tiles
def cam_region(info: Optional[Dict[str, Any]], cam: Optional[Dict[str, Any]],
               frame_w: int, frame_h: int) -> Tuple[int, int, int, int]:
    """The area to show for a facecam: the WHOLE cam rectangle when its outline was found.

    No outline (green-screen cam: just the person cut out over the game, or a cam found by
    its face only): head and shoulders around the face, generous enough that the person is
    not chopped, never a tight face crop.
    """
    if cam and cam.get("rect"):
        return tuple(int(v) for v in cam["rect"])  # type: ignore[return-value]
    fb = (cam or {}).get("face_box") or (info or {}).get("face_box")
    if not fb:
        w, h = int(frame_w * 0.28), int(frame_h * 0.35)
        return (max(0, frame_w - w), 0, w, h)
    fx, fy, fw, fh = fb
    w = int(min(frame_w, max(fw * 3.4, frame_w * 0.16)))
    h = int(min(frame_h, w * 0.9))
    x = int(round(fx + fw / 2.0 - w / 2.0))
    y = int(round(fy - fh * 0.9))
    x = max(0, min(frame_w - w, x))
    y = max(0, min(frame_h - h, y))
    return (x - x % 2, y - y % 2, w - w % 2, h - h % 2)


def cam_tile_geometry(rect: Tuple[int, int, int, int], cell_w: int, max_h: int,
                      max_scale: float) -> Tuple[int, int, float]:
    """Size to show a cam at: its WHOLE picture, enlarged at most `max_scale` times.

    A cam is a small picture in the stream (often ~470x260 px even in 1080p). Stretching a
    slice of it to fill a phone-wide tile (2-3x) is what made cams look like 144p. Instead
    the whole cam is shown at a capped zoom (the rest of the tile is a blurred copy of it).
    Returns (tile_w, tile_h, scale).
    """
    _, _, w, h = rect
    s = min(cell_w / max(1.0, w), max_h / max(1.0, h), max_scale)
    tw = max(2, int(round(w * s)))
    th = max(2, int(round(h * s)))
    return tw - tw % 2, th - th % 2, s


# --------------------------------------------------------------------------- cutout (green-screen) cams
def cutout_region(face: Tuple[int, int, int, int], frame_w: int, frame_h: int) -> Tuple[int, int, int, int]:
    """Head-to-shoulders area for a green-screen cam (the person cut out over the game).

    Wide enough for shoulders and arms, from above the head down to the screen edge when the
    person sits at the bottom of the screen (they usually do)."""
    fx, fy, fw, fh = face
    w = int(min(frame_w * 0.32, max(fw * 2.6, frame_w * 0.16)))
    x = int(round(fx + fw / 2.0 - w / 2.0))
    x = max(0, min(frame_w - w, x))
    y0 = max(0, int(round(fy - fh * 0.75)))
    y1 = frame_h if (fy + fh) > frame_h * 0.5 else min(frame_h, int(fy + fh * 2.8))
    h = max(2, y1 - y0)
    return (x - x % 2, y0 - y0 % 2, w - w % 2, h - h % 2)


def find_cutout_cams(clusters: List[Dict[str, Any]], frames: List[np.ndarray], cams: List[Dict[str, Any]],
                     frame_w: int, frame_h: int, total: int, min_presence: float = 0.5) -> List[Dict[str, Any]]:
    """Cams without a rectangle: a person cut out over the game (green screen).

    A cutout is a big face that stays put (it moves a little, but stays in one area) outside
    every cam rectangle, with the game MOVING around it. A person in a real room (IRL) has a
    still wall around them, so that is not taken as a cam."""
    if len(frames) < 3 or not clusters:
        return []
    M = motion_map(frames, (640, int(round(640 * frame_h / float(frame_w)))))
    sc = 640.0 / frame_w
    out: List[Dict[str, Any]] = []
    for c in clusters:
        fx, fy, fw, fh = c["median_box"]
        # A cutout person is seen at cam size (face ~10-18% of the screen height); bigger faces
        # are IRL close-ups, smaller ones are game characters or the crowd.
        # (a person who leans in towards the camera is bigger, so the limit is looser once a
        # normal cam rectangle proves this is a gaming layout, not an IRL close-up)
        max_face = 0.34 if cams else 0.22
        if c["count"] < min_presence * total or not (0.07 * frame_h <= fh <= max_face * frame_h):
            continue
        cx, cy = fx + fw / 2.0, fy + fh / 2.0
        if any(r["rect"][0] <= cx <= r["rect"][0] + r["rect"][2] and r["rect"][1] <= cy <= r["rect"][1] + r["rect"][3]
               for r in cams + out):
            continue
        centres = [(b[0] + b[2] / 2.0, b[1] + b[3] / 2.0) for _, b in c["boxes"]]
        drift = max(np.std([p[0] for p in centres]) / frame_w, np.std([p[1] for p in centres]) / frame_h)
        if drift > 0.05:
            continue
        rect = cutout_region(c["median_box"], frame_w, frame_h)
        # The game must be moving around the person
        L, T = int(rect[0] * sc), int(rect[1] * sc)
        R, B = int((rect[0] + rect[2]) * sc), int((rect[1] + rect[3]) * sc)
        ring, _ = _ring_motion(M, (L, T, min(R, M.shape[1]), min(B, M.shape[0])))
        if ring < 4.0:
            continue
        cxn, cyn = (rect[0] + rect[2] / 2.0) / frame_w, (rect[1] + rect[3] / 2.0) / frame_h
        out.append({
            "rect": rect, "face_box": c["median_box"], "presence": round(c["count"] / float(total), 3),
            "real_sides": 0, "edge_cov": 0.0, "kind": "cutout",
            "corner": f"{'top' if cyn < 0.5 else 'bottom'}_{'left' if cxn < 0.5 else 'right'}",
        })
    return out


def stream_cam_memory(cams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The parts of each cam worth remembering for the rest of the stream."""
    keep = []
    for c in cams or []:
        kind = c.get("kind") or "box"
        presence = float(c.get("presence", 0.0))
        # Transient cutouts or low-presence faces must not become permanent stream anchors
        if kind == "cutout" and presence < 0.50:
            continue
        keep.append({"rect": list(c["rect"]), "face_box": list(c.get("face_box") or c["rect"]),
                     "kind": kind, "corner": c.get("corner", "unknown")})
    return keep


def merge_known_cams(known: List[Dict[str, Any]], cams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add cams seen in a clip to the stream's list (overlays that overlap an old one are the same)."""
    out = list(known)
    for new in stream_cam_memory([c for c in cams if not c.get("from_stream")]):
        nr = tuple(new["rect"])
        same = False
        for old in out:
            orr = tuple(old["rect"])
            ncx, ncy = nr[0] + nr[2] / 2.0, nr[1] + nr[3] / 2.0
            if _iou(nr, orr) > 0.3 or (orr[0] <= ncx <= orr[0] + orr[2] and orr[1] <= ncy <= orr[1] + orr[3]):
                same = True
                break
        if not same:
            # If an existing known cam is a real box, do not adopt a transient cutout in another corner
            if any(k.get("kind") == "box" for k in out) and new.get("kind") == "cutout":
                continue
            # A stream should have at most 2 known cams (for co-op), never 3 or 4 corners
            if len(out) >= 2:
                continue
            out.append(new)
    return out


def adopt_known_cams(cams: List[Dict[str, Any]], known: List[Dict[str, Any]], detections,
                     frame_w: int, frame_h: int, total: int, min_presence: float = 0.35) -> List[Dict[str, Any]]:
    """Add cams the rest of the stream had but this clip's search missed, when a face is
    really in that spot (so a cam that is not on screen in this part is not invented)."""
    out = list(cams)
    for k in known:
        kr = tuple(int(v) for v in k["rect"])
        kcx, kcy = kr[0] + kr[2] / 2.0, kr[1] + kr[3] / 2.0
        if any(_iou(kr, tuple(c["rect"])) > 0.3 or
               (c["rect"][0] <= kcx <= c["rect"][0] + c["rect"][2] and c["rect"][1] <= kcy <= c["rect"][1] + c["rect"][3])
               for c in out):
            continue
        cutout = k.get("kind") == "cutout"
        hits = []
        if cutout:
            fb = k.get("face_box") or kr
            fcx, fcy = fb[0] + fb[2] / 2.0, fb[1] + fb[3] / 2.0
            for t, b in detections:
                bcx, bcy = b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
                if abs(bcx - fcx) <= 0.12 * frame_w and abs(bcy - fcy) <= 0.20 * frame_h and b[3] >= 0.05 * frame_h:
                    hits.append((t, b))
        else:
            for t, b in detections:
                bcx, bcy = b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
                if kr[0] <= bcx <= kr[0] + kr[2] and kr[1] <= bcy <= kr[1] + kr[3]:
                    hits.append((t, b))
        presence = len({round(t, 2) for t, _ in hits}) / float(max(1, total))
        if presence < min_presence:
            continue
        med = [int(np.median([b[i] for _, b in hits])) for i in range(4)]
        if cutout and len(hits) > 2:
            # a real person stays in one place; faces drifting around are game characters
            sx = np.std([b[0] + b[2] / 2.0 for _, b in hits]) / frame_w
            sy = np.std([b[1] + b[3] / 2.0 for _, b in hits]) / frame_h
            if max(sx, sy) > 0.05:
                continue
        rect = cutout_region(tuple(med), frame_w, frame_h) if cutout else kr
        if any(_iou(rect, tuple(c["rect"])) > 0.3 for c in out):
            continue
        out.append({"rect": tuple(rect), "face_box": tuple(med), "presence": round(presence, 3),
                    "real_sides": 0, "edge_cov": 0.0, "kind": k.get("kind") or "box",
                    "corner": k.get("corner", "unknown"), "from_stream": True})
    return out
