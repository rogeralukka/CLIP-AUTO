"""Quality Check (QC) and Platform Metadata Engine for vertical clips.

Performs empirical verification on rendered clips:
- Duration within campaign limits
- Integrated loudness within +/- 2 LUFS of target (-14 LUFS)
- Black frames detection
- Video freeze detection
- Facecam presence ratio validation
- Music/copyright risk heuristic

Generates platform-ready titles and descriptions for YouTube Shorts, TikTok, Instagram Reels, and X.
"""
import json
import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from .config import load_config
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.qc")


def run_ffmpeg_probe(video_path: str) -> Dict[str, Any]:
    """Inspect video and audio streams using ffprobe or ffmpeg."""
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    ffprobe_bin = ffmpeg_bin.replace("ffmpeg.EXE", "ffprobe.exe").replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")

    if os.path.isfile(ffprobe_bin):
        cmd = [
            ffprobe_bin,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            video_path,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(res.stdout)
        except Exception:
            pass

    return {}


def measure_clip_loudness(video_path: str) -> Optional[float]:
    """Measure integrated loudness (LUFS) of a video file using FFmpeg loudnorm filter."""
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-i", os.path.abspath(video_path),
        "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        start = res.stderr.find("{")
        end = res.stderr.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(res.stderr[start:end+1])
            return float(data.get("input_i", -99.0))
    except Exception as e:
        logger.debug(f"Loudness measurement failed: {e}")
    return None


def detect_black_frames(video_path: str, min_duration: float = 0.5) -> List[Dict[str, float]]:
    """Detect black frame sequences in video using FFmpeg blackdetect."""
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-i", os.path.abspath(video_path),
        "-vf", f"blackdetect=d={min_duration}:pix_th=0.10",
        "-f", "null", "-",
    ]
    periods: List[Dict[str, float]] = []
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        # Parse black_start: 1.234 black_end: 2.345 black_duration: 1.111
        matches = re.finditer(
            r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)",
            res.stderr,
        )
        for m in matches:
            periods.append({
                "start": float(m.group(1)),
                "end": float(m.group(2)),
                "duration": float(m.group(3)),
            })
    except Exception as e:
        logger.debug(f"Blackdetect failed: {e}")
    return periods


def detect_frozen_frames(video_path: str, min_duration: float = 1.0) -> List[Dict[str, float]]:
    """Detect frozen video frames using FFmpeg freezedetect."""
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-i", os.path.abspath(video_path),
        "-vf", f"freezedetect=noise=-60dB:d={min_duration}",
        "-f", "null", "-",
    ]
    periods: List[Dict[str, float]] = []
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        matches = re.finditer(
            r"freeze_start:\s*([0-9.]+)(?:.*?freeze_duration:\s*([0-9.]+)\s+freeze_end:\s*([0-9.]+))?",
            res.stderr,
        )
        for m in matches:
            start_t = float(m.group(1))
            dur = float(m.group(2)) if m.group(2) else 1.0
            end_t = float(m.group(3)) if m.group(3) else (start_t + dur)
            periods.append({"start": start_t, "end": end_t, "duration": dur})
    except Exception as e:
        logger.debug(f"Freezedetect failed: {e}")
    return periods


def perform_qc_check(
    clip_path: str,
    duration: float,
    campaign: Optional[Dict[str, Any]] = None,
    facecam_info: Optional[Dict[str, Any]] = None,
    word_count: int = 0,
    layout: str = "gameplay",
    source_height: int = 0,
) -> Dict[str, Any]:
    """Execute all quality checks on a rendered clip and return structured report."""
    cfg = load_config()
    qc_cfg = cfg.get("qc", {})
    polish_cfg = cfg.get("polish", {})
    target_lufs = polish_cfg.get("loudnorm", {}).get("target_lufs", -14.0)
    lufs_tolerance = qc_cfg.get("loudness_tolerance_lu", 2.0)

    # 1. Duration check
    camp = campaign or {}
    campaign_min = float(camp.get("min_clip_length") or camp.get("min_length_seconds") or 15.0)
    campaign_max = float(camp.get("max_clip_length") or camp.get("max_length_seconds") or 180.0)
    duration_ok = (campaign_min - 0.5) <= duration <= (campaign_max + 0.5)

    # 2. Loudness check
    # A clip with no sound must never pass QC (this used to count silence as "OK").
    measured_lufs = measure_clip_loudness(clip_path)
    audio_ok = measured_lufs is not None and measured_lufs > -60.0
    if audio_ok:
        loudness_ok = abs(measured_lufs - target_lufs) <= lufs_tolerance
    else:
        loudness_ok = False
        measured_lufs = -99.0

    # 3. Black frames check (ignore black frames right at start <0.3s or end >dur-0.3s)
    black_periods = detect_black_frames(clip_path, min_duration=0.5)
    mid_black = [
        b for b in black_periods
        if b["start"] > 0.4 and b["end"] < (duration - 0.4)
    ]
    black_frames_ok = len(mid_black) == 0

    # 4. Frozen frames check
    frozen_periods = detect_frozen_frames(clip_path, min_duration=1.0)
    mid_frozen = [
        f for f in frozen_periods
        if f["start"] > 0.5 and f["end"] < (duration - 0.5)
    ]
    frozen_frames_ok = len(mid_frozen) == 0

    # 5. Facecam presence
    presence_ratio = facecam_info.get("presence_ratio", 0.0) if facecam_info else 0.0
    facecam_ok = True
    if layout in ("split", "duo"):
        facecam_ok = presence_ratio >= 0.50

    # 6. Possible music / dead air heuristic
    possible_music = False
    if duration >= 15.0 and word_count <= 2 and measured_lufs > -24.0:
        possible_music = True

    # Compute overall badge status
    flags: List[str] = []
    if not duration_ok:
        flags.append(f"Duration {duration:.1f}s outside bounds ({campaign_min}s-{campaign_max}s)")
    if not audio_ok:
        flags.append("No sound in the clip")
    elif not loudness_ok:
        flags.append(f"Loudness {measured_lufs:.1f} LUFS outside target ({target_lufs} +/- {lufs_tolerance} LU)")
    if not black_frames_ok:
        flags.append(f"Detected {len(mid_black)} black frame sequence(s)")
    if not frozen_frames_ok:
        flags.append(f"Detected {len(mid_frozen)} frozen frame sequence(s)")
    if not facecam_ok:
        flags.append(f"Split layout used but facecam presence is low ({presence_ratio:.1%})")
    if source_height and source_height < 720:
        flags.append(f"Source video was only {source_height}p from YouTube, so this clip looks soft")
    if possible_music:
        flags.append("Possible copyrighted music / no speech detected during loud audio")

    if not audio_ok or not duration_ok or not black_frames_ok or not frozen_frames_ok:
        status = "FAIL"
    elif flags:
        status = "WARN"
    else:
        status = "PASS"

    report = {
        "status": status,
        "duration_ok": duration_ok,
        "loudness_ok": loudness_ok,
        "audio_ok": audio_ok,
        "black_frames_ok": black_frames_ok,
        "frozen_frames_ok": frozen_frames_ok,
        "facecam_present": facecam_ok,
        "possible_music": possible_music,
        "measured_lufs": round(measured_lufs, 2),
        "target_lufs": target_lufs,
        "presence_ratio": presence_ratio,
        "source_height": source_height,
        "flags": flags,
    }

    logger.info(f"QC for {os.path.basename(clip_path)}: status={status}, flags={len(flags)}")
    return report


def generate_platform_metadata(
    hook_title: str,
    transcript: str,
    campaign: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Generate platform-specific title, description, and hashtags for YouTube Shorts, TikTok, Reels, X."""
    title = (hook_title or "").strip()
    hashtags_list = campaign.get("required_hashtags", []) if campaign else []
    disclosure = campaign.get("disclosure_text", "") if campaign else ""

    # Ensure hashtags have '#'
    formatted_tags = [f"#{t.lstrip('#')}" for t in hashtags_list]
    tag_str = " ".join(formatted_tags)

    # YouTube Shorts: Title <100 chars
    yt_tag = "#Shorts"
    combined_tags = f"{yt_tag} {tag_str}".strip()
    if title:
        max_title_len = max(10, 95 - len(combined_tags))
        clean_yt_title = title[:max_title_len].strip()
        yt_full_title = f"{clean_yt_title} {combined_tags}".strip()
    else:
        yt_full_title = combined_tags

    desc_parts = []
    if title:
        desc_parts.append(title)
    if transcript:
        desc_parts.append(f"{transcript[:300]}...")
    if tag_str:
        desc_parts.append(tag_str)
    if disclosure:
        desc_parts.append(disclosure)
    yt_description = "\n\n".join(desc_parts).strip()

    # TikTok: Caption < 150 chars
    tt_suffix = f" {tag_str} {disclosure}".strip()
    if title:
        max_tt_title = max(10, 145 - len(tt_suffix))
        tt_caption = f"{title[:max_tt_title].strip()} {tt_suffix}".strip()
    else:
        tt_caption = tt_suffix

    # Instagram Reels: Caption
    reels_parts = []
    if title:
        reels_parts.append(title)
        reels_parts.append(".")
    if tag_str:
        reels_parts.append(tag_str)
    if disclosure:
        reels_parts.append(disclosure)
    reels_caption = "\n".join(reels_parts).strip()

    # X (Twitter): Tweet < 280 chars
    x_suffix = f"\n\n{tag_str} {disclosure}".strip()
    if title:
        max_x_title = max(10, 275 - len(x_suffix))
        x_tweet = f"{title[:max_x_title].strip()} {x_suffix}".strip()
    else:
        x_tweet = x_suffix.strip()

    return {
        "youtube_title": yt_full_title,
        "youtube_description": yt_description,
        "tiktok_caption": tt_caption,
        "reels_caption": reels_caption,
        "x_tweet": x_tweet,
        "hashtags": tag_str,
        "disclosure": disclosure,
    }
