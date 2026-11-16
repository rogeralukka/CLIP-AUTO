"""Moment detection algorithms: chat-spike detection and audio loudness fallback with preview snippets."""
import logging
import math
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .process_manager import check_cancelled, run_tracked_command
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.detection")


class Moment(dict):
    """Dictionary representing a detected moment that also supports 3-tuple unpacking (start, end, score)."""
    def __iter__(self):
        yield self["start"]
        yield self["end"]
        yield self["score"]


def spike_moments(
    comments: List[Dict[str, Any]],
    window: int = 5,
    z_thresh: float = 2.0,
    min_msgs: int = 10,
    before: int = 20,
    after: int = 30,
    merge_gap: int = 15,
    top: Optional[int] = None,
    safety_max: int = 50,
) -> List[Dict[str, Any]]:
    """Detect hype moments from chat comments using rolling-window z-score analysis.

    Returns a list of dicts:
    [{"start": int, "end": int, "score": float, "preview": str, "messages_in_window": int}]
    """
    valid_comments = [
        c for c in comments
        if isinstance(c, dict) and c.get("content_offset_seconds") is not None
    ]
    if not valid_comments:
        logger.warning("No chat comments with valid timestamps found.")
        return []

    valid_comments.sort(key=lambda c: c["content_offset_seconds"])
    times = [c["content_offset_seconds"] for c in valid_comments]

    end = int(times[-1]) + 1
    per_sec = [0] * end
    msgs_by_sec: Dict[int, List[str]] = {s: [] for s in range(end)}

    for c in valid_comments:
        idx = int(c["content_offset_seconds"])
        if 0 <= idx < end:
            per_sec[idx] += 1
            body = c.get("message", {}).get("body", "").strip()
            if body:
                msgs_by_sec[idx].append(body)

    csum = [0]
    for v in per_sec:
        csum.append(csum[-1] + v)

    num_windows = end - window + 1
    if num_windows <= 0:
        logger.warning("Chat duration is shorter than window size.")
        return []

    wins = [(s, csum[s + window] - csum[s]) for s in range(num_windows)]
    counts = [n for _, n in wins]
    mean = sum(counts) / len(counts)
    std = math.sqrt(sum((c - mean) ** 2 for c in counts) / len(counts)) or 1.0

    peaks = [
        (s, n, (n - mean) / std)
        for s, n in wins
        if (n - mean) / std >= z_thresh and n >= min_msgs
    ]
    peaks.sort(key=lambda p: p[0])

    merged: List[Tuple[int, int, float]] = []
    for s, n, z in peaks:
        if merged and s - merged[-1][0] <= merge_gap:
            if z > merged[-1][2]:
                merged[-1] = (s, n, z)
        else:
            merged.append((s, n, z))

    merged.sort(key=lambda p: -p[2])

    # Limit to top N if in MANUAL mode; otherwise cap at safety_max in AUTO mode
    limit = top if (top is not None and top > 0) else safety_max
    selected_peaks = merged[:limit]

    results: List[Dict[str, Any]] = []
    for s, n, z in selected_peaks:
        st = max(0, s - before)
        et = min(end, s + window + after)

        # Collect sample chat preview words
        sample_words = []
        for sec in range(s, min(end, s + window)):
            sample_words.extend(msgs_by_sec.get(sec, []))

        preview_str = ", ".join(sample_words[:5]) if sample_words else f"{n} chat messages in {window}s"
        results.append(Moment(
            start=st,
            end=et,
            score=round(z, 2),
            preview=f"Chat Burst: {preview_str}",
            messages_in_window=n,
        ))

    results.sort(key=lambda p: p["start"])
    logger.info(f"Chat spike detector identified {len(results)} hype moments.")
    return results


def detect_audio_loudness_moments(
    video_path: str,
    window: int = 6,
    z_thresh: float = 1.8,
    min_duration: int = 15,
    max_duration: int = 60,
    before: int = 15,
    after: int = 25,
    merge_gap: int = 15,
    top: Optional[int] = None,
    safety_max: int = 50,
    job_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fallback moment detection: extracts momentary loudness (LUFS) using FFmpeg ebur128."""
    check_cancelled(job_id)
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", video_path,
        "-vn",
        "-af", "ebur128=peak=none",
        "-f", "null",
        "-"
    ]

    logger.info(f"Analyzing audio loudness peaks with FFmpeg ebur128 on {video_path}...")
    lines: List[str] = []

    def on_line(l: str):
        lines.append(l)

    ret = run_tracked_command(cmd, job_id=job_id, line_callback=on_line)
    check_cancelled(job_id)

    pattern = re.compile(r"t:\s*([\d\.]+)\s+TARGET:[^M]+M:\s*([-\d\.]+)")
    samples: List[Tuple[float, float]] = []

    for line in lines:
        m = pattern.search(line)
        if m:
            try:
                t = float(m.group(1))
                m_val = float(m.group(2))
                if m_val > -70.0:
                    samples.append((t, m_val))
            except ValueError:
                continue

    if not samples:
        logger.warning("No audio loudness samples could be extracted.")
        return []

    duration = int(samples[-1][0]) + 1
    per_sec = [-70.0] * duration
    for t, m_val in samples:
        sec = int(t)
        if 0 <= sec < duration:
            if m_val > per_sec[sec]:
                per_sec[sec] = m_val

    min_val = min(v for v in per_sec if v > -70.0) if any(v > -70.0 for v in per_sec) else -70.0
    for i in range(duration):
        if per_sec[i] <= -70.0:
            per_sec[i] = min_val

    pos_loudness = [max(0.0, v - min_val) for v in per_sec]
    if not pos_loudness:
        return []

    num_windows = duration - window + 1
    if num_windows <= 0:
        return []

    csum = [0.0]
    for v in pos_loudness:
        csum.append(csum[-1] + v)

    wins = [(s, csum[s + window] - csum[s]) for s in range(num_windows)]
    scores = [sc for _, sc in wins]
    mean = sum(scores) / len(scores)
    std = math.sqrt(sum((c - mean) ** 2 for c in scores) / len(scores)) or 1.0

    peaks = [
        (s, sc, (sc - mean) / std)
        for s, sc in wins
        if (sc - mean) / std >= z_thresh
    ]
    peaks.sort(key=lambda p: p[0])

    merged: List[Tuple[int, int, float]] = []
    for s, sc, z in peaks:
        if merged and s - merged[-1][0] <= merge_gap:
            if z > merged[-1][2]:
                merged[-1] = (s, sc, z)
        else:
            merged.append((s, sc, z))

    merged.sort(key=lambda p: -p[2])
    limit = top if (top is not None and top > 0) else safety_max
    selected_peaks = merged[:limit]

    results: List[Dict[str, Any]] = []
    for s, sc, z in selected_peaks:
        st = max(0, s - before)
        et = min(duration, s + window + after)
        if et - st > max_duration:
            et = st + max_duration
        if et - st < min_duration:
            et = min(duration, st + min_duration)

        results.append(Moment(
            start=st,
            end=et,
            score=round(z, 2),
            preview=f"Audio Peak Volume (intensity z={z:.2f})",
            messages_in_window=0,
        ))

    results.sort(key=lambda p: p["start"])
    logger.info(f"Audio loudness fallback identified {len(results)} peak moments.")
    return results


def find_moments(
    comments: Optional[List[Dict[str, Any]]] = None,
    video_path: Optional[str] = None,
    window: int = 5,
    z_thresh: float = 2.0,
    min_msgs: int = 10,
    before: int = 20,
    after: int = 30,
    merge_gap: int = 15,
    top: Optional[int] = None,
    safety_max: int = 50,
    job_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Find hype moments from chat spikes or audio loudness fallback."""
    check_cancelled(job_id)
    if comments and len(comments) >= min_msgs:
        moments = spike_moments(
            comments,
            window=window,
            z_thresh=z_thresh,
            min_msgs=min_msgs,
            before=before,
            after=after,
            merge_gap=merge_gap,
            top=top,
            safety_max=safety_max,
        )
        if moments:
            return moments
        logger.info("Chat spike detection yielded 0 moments; trying audio loudness fallback...")

    if video_path:
        return detect_audio_loudness_moments(
            video_path,
            window=max(5, window),
            z_thresh=max(1.5, z_thresh - 0.3),
            before=before,
            after=after,
            merge_gap=merge_gap,
            top=top,
            safety_max=safety_max,
            job_id=job_id,
        )

    logger.warning("No chat data or video path available for moment detection.")
    return []
