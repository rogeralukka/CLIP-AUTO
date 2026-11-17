"""Moment detection algorithms: composite hype curve, robust z-scores, smart boundaries, and deduplication."""
import json
import logging
import math
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .config import load_config
from .process_manager import check_cancelled, run_tracked_command
from .signals import (
    build_hype_curve,
    dedupe_moments,
    snap_moment_boundaries,
)
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.detection")


class Moment(dict):
    """Dictionary representing a detected moment that also supports 3-tuple unpacking (start, end, score)."""
    def __iter__(self):
        yield self["start"]
        yield self["end"]
        yield self["score"]


def get_media_duration_seconds(media_path: str) -> float:
    """Extract media duration in seconds using ffprobe or ffmpeg."""
    if not media_path or not os.path.isfile(media_path):
        return 0.0

    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    ffprobe_bin = ffmpeg_bin.replace("ffmpeg.exe", "ffprobe.exe") if ffmpeg_bin.endswith("ffmpeg.exe") else "ffprobe"

    if shutil_which(ffprobe_bin):
        cmd = [
            ffprobe_bin, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            media_path
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip())
        except Exception:
            pass

    # Fallback to ffmpeg -i inspection
    cmd = [ffmpeg_bin, "-i", media_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d\.]+)", res.stderr)
        if m:
            h, mins, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600 + mins * 60 + s
    except Exception:
        pass

    return 0.0


def shutil_which(cmd: str) -> Optional[str]:
    import shutil
    return shutil.which(cmd)


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
    """Legacy chat spike detector for direct tests and baseline backward compatibility."""
    valid_comments = [
        c for c in comments
        if isinstance(c, dict) and c.get("content_offset_seconds") is not None
    ]
    if not valid_comments:
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
    limit = top if (top is not None and top > 0) else safety_max
    selected_peaks = merged[:limit]

    results: List[Dict[str, Any]] = []
    for s, n, z in selected_peaks:
        st = max(0, s - before)
        et = min(end, s + window + after)

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
            breakdown={"chat_rate": round(z, 2)},
        ))

    results.sort(key=lambda p: p["start"])
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
    """Legacy audio loudness peak detector for direct tests and baseline backward compatibility."""
    check_cancelled(job_id)
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    cmd = [
        ffmpeg_bin, "-y",
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
            breakdown={"streamer_loudness": round(z, 2)},
        ))

    results.sort(key=lambda p: p["start"])
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
    full_transcript_data: Optional[Dict[str, Any]] = None,
    heatmap_data: Optional[Any] = None,
    vod_id: Optional[str] = None,
    min_clip_length: float = 15.0,
    max_clip_length: float = 60.0,
) -> List[Dict[str, Any]]:
    """Phase 2: Composite Hype Curve with robust z-scores, smart boundaries, and deduplication."""
    check_cancelled(job_id)
    cfg = load_config()

    # Determine stream duration
    duration = 0.0
    if full_transcript_data and full_transcript_data.get("duration"):
        duration = float(full_transcript_data["duration"])
    elif video_path and os.path.isfile(video_path):
        duration = get_media_duration_seconds(video_path)

    if duration <= 0.0 and comments:
        valid_offsets = [c.get("content_offset_seconds") for c in comments if c.get("content_offset_seconds") is not None]
        if valid_offsets:
            duration = max(valid_offsets) + 10.0

    if duration <= 0.0:
        duration = 180.0

    dur_int = int(math.ceil(duration))

    # Build multi-signal composite hype curve
    composite_curve, breakdown_z, msgs_by_sec = build_hype_curve(
        duration=dur_int,
        comments=comments,
        media_path=video_path,
        full_transcript_data=full_transcript_data,
        heatmap_data=heatmap_data,
        window_size=cfg.get("signals", {}).get("rolling_window_seconds", 600),
        job_id=job_id,
    )
    check_cancelled(job_id)

    # Rolling window smoothing over composite curve
    win = max(2, window)
    csum = np.cumsum(np.insert(composite_curve, 0, 0.0))
    window_scores = (csum[win:] - csum[:-win]) / float(win)

    peaks: List[Tuple[int, float]] = []
    for s_idx, sc in enumerate(window_scores):
        if sc >= z_thresh:
            peaks.append((s_idx, float(sc)))

    # Fallback to chat spike detector or audio loudness detector if composite curve has 0 peaks
    if not peaks:
        logger.info(f"Composite hype curve found 0 moments at z>={z_thresh:.1f}; evaluating fallback paths...")
        if comments and len(comments) >= min_msgs:
            legacy_spikes = spike_moments(
                comments, window=window, z_thresh=z_thresh, min_msgs=min_msgs,
                before=before, after=after, merge_gap=merge_gap, top=top, safety_max=safety_max
            )
            if legacy_spikes:
                return legacy_spikes

        if video_path and os.path.isfile(video_path):
            legacy_loudness = detect_audio_loudness_moments(
                video_path, window=max(5, window), z_thresh=max(1.5, z_thresh - 0.3),
                before=before, after=after, merge_gap=merge_gap, top=top, safety_max=safety_max,
                job_id=job_id
            )
            if legacy_loudness:
                return legacy_loudness

        return []

    # Merge nearby peaks within merge_gap
    peaks.sort(key=lambda p: p[0])
    merged_peaks: List[Tuple[int, float]] = []
    gap = cfg.get("signals", {}).get("merge_gap_seconds", merge_gap)

    for p_sec, sc in peaks:
        if merged_peaks and p_sec - merged_peaks[-1][0] <= gap:
            if sc > merged_peaks[-1][1]:
                merged_peaks[-1] = (p_sec, sc)
        else:
            merged_peaks.append((p_sec, sc))

    # Sort peaks by score descending
    merged_peaks.sort(key=lambda p: -p[1])
    limit = top if (top is not None and top > 0) else cfg.get("signals", {}).get("safety_max_clips", safety_max)
    selected_peaks = merged_peaks[:limit]

    # Smart boundaries & Moment creation
    candidate_moments: List[Dict[str, Any]] = []
    max_lead = cfg.get("signals", {}).get("max_lead_seconds", 5.0)
    max_trail = cfg.get("signals", {}).get("max_trail_seconds", 5.0)

    for p_sec, sc in selected_peaks:
        peak_center = float(p_sec + win / 2.0)

        # Smart sentence boundary snapping
        st, et = snap_moment_boundaries(
            peak_time=peak_center,
            stream_duration=duration,
            full_transcript_data=full_transcript_data,
            min_length=min_clip_length,
            max_length=max_clip_length,
            max_lead=max_lead,
            max_trail=max_trail,
        )

        # Build signal breakdown dictionary for this peak second
        p_idx = min(dur_int - 1, max(0, int(peak_center)))
        sig_breakdown = {
            sig_name: round(float(z_arr[p_idx]), 2)
            for sig_name, z_arr in breakdown_z.items()
        }

        # Preview snippet
        preview_text = ""
        sample_msgs = []
        for s in range(max(0, p_idx - 3), min(dur_int, p_idx + 4)):
            sample_msgs.extend(msgs_by_sec.get(s, []))
        if sample_msgs:
            preview_text = f"Chat: {', '.join(sample_msgs[:4])}"

        if not preview_text and full_transcript_data:
            from .transcription import get_transcript_text_in_window
            t_txt = get_transcript_text_in_window(full_transcript_data, st, et)
            if t_txt:
                preview_text = f"Quote: \"{t_txt[:60]}...\""

        if not preview_text:
            preview_text = f"Hype Spike (score={sc:.2f})"

        candidate_moments.append(Moment(
            start=st,
            end=et,
            peak_second=p_idx,
            score=round(sc, 2),
            preview=preview_text,
            breakdown=sig_breakdown,
        ))

    # Deduplication against downloads/<vod_id>/clipped_moments.json
    target_vod_id = vod_id or "default"
    deduped = dedupe_moments(
        candidate_moments,
        vod_id=target_vod_id,
        overlap_threshold=cfg.get("signals", {}).get("dedupe_overlap_threshold", 0.50),
        downloads_dir="downloads",
    )

    deduped.sort(key=lambda m: m["start"])
    logger.info(f"Composite detection found {len(deduped)} moments after boundary snapping and deduplication.")
    return deduped
