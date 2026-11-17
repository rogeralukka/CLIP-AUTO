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
        """Detect faces in a BGR frame. Returns list of (x, y, w, h) bounding boxes."""
        h, w = frame_bgr.shape[:2]
        boxes: List[Tuple[int, int, int, int]] = []

        if self._backend == "mediapipe" and self._mp_detector is not None:
            try:
                import mediapipe as mp
                rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                results = self._mp_detector.detect(mp_img)
                for det in results.detections:
                    bb = det.bounding_box
                    # Clamp to frame bounds
                    bx = max(0, int(bb.origin_x))
                    by = max(0, int(bb.origin_y))
                    bw = min(w - bx, int(bb.width))
                    bh = min(h - by, int(bb.height))
                    if bw > 15 and bh > 15:
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

    # Pick cluster with most detections
    clusters.sort(key=lambda c: len(c), reverse=True)
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
        "count": len(best_cluster),
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

    # Initial padded box
    x0 = max(0, x - pad_w)
    y0 = max(0, y - pad_h)
    x1 = min(frame_width, x + w + pad_w)
    y1 = min(frame_height, y + h + pad_h)

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
) -> Dict[str, Any]:
    """Detect streamer facecam across video duration and select layout.

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
        boxes = detector.detect_faces(frame)
        if boxes:
            frames_with_face += 1
            for b in boxes:
                all_detections.append((t, b))

    total_samples = len(samples)
    cluster = cluster_face_boxes(all_detections, frame_w, frame_h)

    if cluster is None:
        return {
            "found": False,
            "layout": "gameplay",
            "presence_ratio": 0.0,
            "face_box": None,
            "padded_box": None,
            "corner": "unknown",
            "frame_size": (frame_w, frame_h),
            "total_samples": total_samples,
            "detected_samples": 0,
        }

    cluster_count = cluster["count"]
    presence_ratio = round(cluster_count / max(1, total_samples), 3)
    found = presence_ratio >= thresh

    med_box = cluster["median_box"]
    padded_box = expand_box_with_padding(med_box, frame_w, frame_h, padding)
    layout = "split" if found else "gameplay"

    logger.info(
        f"Facecam analysis: found={found}, presence={presence_ratio:.1%} ({cluster_count}/{total_samples}), "
        f"layout={layout}, corner={cluster['corner']}, box={med_box}"
    )

    return {
        "found": found,
        "layout": layout,
        "presence_ratio": presence_ratio,
        "face_box": med_box,
        "padded_box": padded_box,
        "corner": cluster["corner"],
        "frame_size": (frame_w, frame_h),
        "total_samples": total_samples,
        "detected_samples": cluster_count,
    }


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
    if "right" in face_corner:
        # Bias left: place crop at 10% from left
        x = max(0, int((frame_width - crop_w) * 0.25))
    elif "left" in face_corner:
        # Bias right: place crop at 75% from left
        x = min(frame_width - crop_w, int((frame_width - crop_w) * 0.75))
    else:
        # Center crop
        x = (frame_width - crop_w) // 2

    y = (frame_height - crop_h) // 2
    return (x, y, crop_w, crop_h)
