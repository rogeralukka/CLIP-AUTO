"""Video cutting and vertical 9:16 rendering with tracked processes and NVENC."""
import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .captions import escape_ffmpeg_ass_path
from .config import load_config
from .detection import get_media_duration_seconds
from .upscale import needs_upscale, upscale_crop_video, upscale_enabled
from .facecam import (cam_region, cam_tile_geometry, detect_facecam, gameplay_crop_avoiding)
from .process_manager import check_cancelled_or_paused, run_tracked_command
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.cutter")


def get_video_encoder_flags(use_cpu: bool = False, intermediate: bool = False) -> List[str]:
    """FFmpeg video encoder settings for NVENC (GPU) or libx264 (CPU).

    Final clips use high-quality VBR with +faststart for instant zero-buffering web playback.
    `intermediate` is for the temporary rough cut: near-lossless so the final
    render doesn't stack a second round of compression on top.
    """
    if use_cpu:
        if intermediate:
            return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14"]
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high",
                "-movflags", "+faststart"]
    if intermediate:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "15", "-b:v", "0"]
    return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "21", "-b:v", "0",
            "-maxrate", "12M", "-bufsize", "24M", "-profile:v", "high", "-spatial-aq", "1",
            "-temporal-aq", "1", "-rc-lookahead", "32", "-bf", "3", "-movflags", "+faststart"]


def get_loudnorm_audio_filter(
    source_media: str,
    target_lufs: float = -14.0,
    target_tp: float = -1.0,
    target_lra: float = 11.0,
) -> Optional[str]:
    """Measure two-pass loudness parameters and return loudnorm audio filter string.

    If audio is already within +/- 1.0 LUFS of target and true peak <= target_tp,
    returns None to avoid unnecessary re-compression.
    """
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-i", os.path.abspath(source_media),
        "-af", f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}:print_format=json",
        "-f", "null", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        start = res.stderr.find("{")
        end = res.stderr.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(res.stderr[start:end+1])
            input_i = float(data.get("input_i", -99.0))
            input_tp = float(data.get("input_tp", 0.0))
            input_lra = float(data.get("input_lra", 0.0))
            input_thresh = float(data.get("input_thresh", 0.0))
            target_offset = float(data.get("target_offset", 0.0))

            if abs(input_i - target_lufs) <= 1.0 and input_tp <= target_tp:
                logger.info(
                    f"Audio loudness ({input_i:.1f} LUFS) is already at target ({target_lufs:.1f}); skipping loudnorm."
                )
                return None

            return (
                f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}:"
                f"measured_I={input_i}:measured_TP={input_tp}:measured_LRA={input_lra}:"
                f"measured_thresh={input_thresh}:offset={target_offset}:linear=true"
            )
    except Exception as e:
        logger.debug(f"Failed to measure loudnorm pass 1: {e}")

    return None


def _decode_envelope(media: str, start: float, duration: float, rate: int = 100) -> Optional[np.ndarray]:
    """Decode mono audio and return a loudness envelope sampled `rate` times per second."""
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    sr = 8000
    cmd = [ffmpeg_bin, "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}",
           "-i", os.path.abspath(media), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        return None
    if res.returncode != 0 or not res.stdout:
        return None
    pcm = np.frombuffer(res.stdout, dtype=np.int16).astype(np.float32)
    hop = sr // rate
    n = len(pcm) // hop
    if n < rate:  # under a second of audio
        return None
    return np.abs(pcm[: n * hop]).reshape(n, hop).mean(axis=1)


def find_section_audio_offset(section_path: str, stream_audio: str, section_start: float,
                              duration: float, search: float = 4.0) -> float:
    """How far (seconds) the downloaded video section is from where we asked it to start.

    Section downloads can land a little off the requested time (keyframes, Twitch segments).
    We line the section's own sound up against the full-stream audio we already have, so
    the picture, the sound and the captions all match. Returns 0.0 when unsure.
    """
    probe = min(duration, 30.0)
    sec_env = _decode_envelope(section_path, 0.0, probe)
    if sec_env is None or float(np.max(sec_env)) < 30.0:  # no usable sound in the section
        return 0.0
    ref_start = max(0.0, section_start - search)
    lead = section_start - ref_start
    ref_env = _decode_envelope(stream_audio, ref_start, probe + lead + search)
    if ref_env is None or len(ref_env) <= len(sec_env):
        return 0.0
    a = sec_env - sec_env.mean()
    a_norm = float(np.linalg.norm(a)) or 1.0
    best_lag, best_corr = 0, -1.0
    for lag in range(0, len(ref_env) - len(a) + 1):
        b = ref_env[lag: lag + len(a)]
        b = b - b.mean()
        c = float(np.dot(a, b)) / (a_norm * (float(np.linalg.norm(b)) or 1.0))
        if c > best_corr:
            best_corr, best_lag = c, lag
    if best_corr < 0.5:
        return 0.0
    return round(best_lag / 100.0 - lead, 2)


def attach_stream_audio(section_path: str, stream_audio: str, section_start: float,
                        output_path: str, job_id: Optional[str] = None) -> "tuple[str, float]":
    """Replace a section's sound with the matching part of the full-stream audio.

    The full-stream audio is what Whisper heard and what the Review page plays, so it is
    known-good. Section downloads sometimes come back silent or without an audio track,
    which gave clips with no sound. This makes the clip's sound come from the source that
    definitely works, lined up with the picture.

    Returns (path, delta): delta is how many seconds later than requested the section's
    picture really starts, so the caller can shift its cut and the captions stay in sync.
    """
    side = os.path.splitext(os.path.abspath(output_path))[0] + ".json"
    if os.path.isfile(output_path) and os.path.getsize(output_path) > 1024 and os.path.isfile(side):
        try:
            with open(side, "r", encoding="utf-8") as f:
                return os.path.abspath(output_path), float(json.load(f).get("delta", 0.0))
        except Exception:
            pass
    dur = float(get_media_duration_seconds(section_path) or 0.0)
    if dur <= 0.5 or not (stream_audio and os.path.isfile(stream_audio)):
        return section_path, 0.0
    offset = find_section_audio_offset(section_path, stream_audio, section_start, dur)
    if offset:
        logger.info(f"Section starts {offset:+.2f}s from the requested time; lining audio up.")
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    cmd = [
        ffmpeg_bin, "-y",
        "-i", os.path.abspath(section_path),
        "-ss", f"{max(0.0, section_start + offset):.3f}", "-t", f"{dur:.3f}",
        "-i", os.path.abspath(stream_audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest", os.path.abspath(output_path),
    ]
    ret = run_tracked_command(cmd, job_id=job_id)
    if ret != 0 or not os.path.isfile(output_path):
        logger.warning("Could not attach stream audio; keeping the section's own audio.")
        return section_path, 0.0
    try:
        with open(side, "w", encoding="utf-8") as f:
            json.dump({"delta": offset}, f)
    except Exception:
        pass
    return os.path.abspath(output_path), offset


def cut_rough_clip(
    source_video: str,
    start_sec: float,
    end_sec: float,
    output_path: str,
    use_cpu: bool = False,
    job_id: Optional[str] = None,
) -> str:
    """Cut an accurate sub-clip from the source video by re-encoding."""
    check_cancelled_or_paused(job_id)
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    duration = max(1.0, end_sec - start_sec)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    encoder_flags = get_video_encoder_flags(use_cpu, intermediate=True)

    cmd = [
        ffmpeg_bin, "-y",
        "-ss", str(round(start_sec, 3)),
        "-i", os.path.abspath(source_video),
        "-t", str(round(duration, 3)),
        *encoder_flags,
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        os.path.abspath(output_path)
    ]

    logger.info(f"Cutting rough clip ({start_sec:.1f}s -> {end_sec:.1f}s, dur={duration:.1f}s) -> {output_path}")

    try:
        ret = run_tracked_command(cmd, job_id=job_id)
        if ret != 0:
            raise RuntimeError(f"FFmpeg accurate cut exited with code {ret}")
    except Exception as e:
        check_cancelled_or_paused(job_id)
        if not use_cpu:
            logger.warning(f"NVENC cut failed ({e}). Retrying with libx264...")
            return cut_rough_clip(source_video, start_sec, end_sec, output_path, use_cpu=True, job_id=job_id)
        raise RuntimeError(f"FFmpeg accurate cut failed: {e}")

    check_cancelled_or_paused(job_id)
    return os.path.abspath(output_path)


def build_pan_expression(track: List[Any], crop_w: int, src_w: int) -> str:
    """FFmpeg expression for the crop's x position over time, from (t, centre_x) points.

    Piecewise-linear between points so the crop glides instead of jumping.
    """
    max_x = max(0, src_w - crop_w)
    pts = []
    for t, cx in track:
        x = int(min(max_x, max(0, float(cx) - crop_w / 2.0)))
        if pts and abs(float(t) - pts[-1][0]) < 0.2:
            continue
        pts.append((round(float(t), 2), x))
    if not pts:
        return str(max_x // 2)
    if len(pts) > 90:  # keep the expression a sane size
        step = len(pts) / 90.0
        pts = [pts[int(i * step)] for i in range(90)] + [pts[-1]]
    expr = str(pts[-1][1])
    for i in range(len(pts) - 2, -1, -1):
        (t0, x0), (t1, x1) = pts[i], pts[i + 1]
        dt = max(0.01, t1 - t0)
        expr = f"if(lt(t,{t1}),{x0}+({x1 - x0})*(t-{t0})/{dt:.2f},{expr})"
    return f"if(lt(t,{pts[0][0]}),{pts[0][1]},{expr})"


TOP_BAND, MID_BAND = 192, 1536   # hook band; the 8 middle bands (the last 192 is the caption band)


def _read_emoji_events(ass_path: Optional[str]) -> List[Dict[str, Any]]:
    """Emoji pictures planned next to the caption file (see captions.make_ass)."""
    if not ass_path:
        return []
    side = os.path.splitext(ass_path)[0] + ".emoji.json"
    try:
        with open(side, "r", encoding="utf-8") as f:
            events = json.load(f)
        return [e for e in events if os.path.isfile(e.get("path", ""))]
    except (OSError, ValueError):
        return []


def _cam_max_scale() -> float:
    pol = load_config().get("polish", {})
    ai_on = upscale_enabled()
    return float(pol.get("cam_max_upscale_ai" if ai_on else "cam_max_upscale", 2.2 if ai_on else 1.5))


def plan_cam_band(layout: str, facecam_info: Optional[Dict[str, Any]],
                  cam_max_scale: Optional[float] = None) -> Optional[int]:
    """Height of the cam section for the cam + game layouts ('split', 'duo'); None for a
    single picture. The same numbers drive the picture and the caption position, so the
    captions can sit exactly on the border between the sections."""
    info = facecam_info or {}
    cams = list(info.get("cams") or [])
    src_w, src_h = info.get("frame_size", (1920, 1080))
    scale = cam_max_scale if cam_max_scale is not None else _cam_max_scale()
    if layout == "duo" and len(cams) >= 2:
        a, b = sorted(cams[:2], key=lambda c: c["rect"][0])
        th_a = cam_tile_geometry(tuple(a["rect"]), 540, 700, scale)[1]
        th_b = cam_tile_geometry(tuple(b["rect"]), 540, 700, scale)[1]
        return max(300, min(700, max(th_a, th_b)))
    if layout in ("split", "duo"):
        rect = cam_region(info, cams[0] if cams else None, src_w, src_h)
        return max(360, min(800, cam_tile_geometry(rect, 1080, 800, scale)[1]))
    return None


def caption_anchor_y(layout: str, facecam_info: Optional[Dict[str, Any]]) -> int:
    """Vertical centre (px of 1920) for the captions: on the border between the cam section(s)
    and the game, or just below the middle when the clip is a single picture."""
    cam_h = plan_cam_band(layout, facecam_info)
    if cam_h is not None:
        return TOP_BAND + cam_h
    if layout in ("gameplay", "auto"):
        # Framed picture (16:9 in the middle): captions go in the blurred band just under it,
        # never over the action
        return 1400
    return 1500


def build_punch_zoom_filter(
    clip_duration: float,
    punch_start_sec: Optional[float] = None,
    punch_end_sec: Optional[float] = None,
    zoom_factor: float = 1.12,
) -> Optional[str]:
    """Generate subtle punch-zoom filter on punchline climax (1.10x - 1.15x).

    If punch_start_sec and punch_end_sec are not specified, defaults to the final
    climax beat (last 3.5 seconds of the clip, e.g. dur-3.5 to dur-0.5).
    """
    if clip_duration < 6.0:
        return None

    zf = max(1.05, min(1.20, float(zoom_factor)))
    p_start = punch_start_sec if punch_start_sec is not None else max(0.0, clip_duration - 4.0)
    p_end = punch_end_sec if punch_end_sec is not None else max(p_start + 1.0, clip_duration - 0.5)

    if p_end <= p_start:
        return None

    return (
        f"crop=w='if(between(t,{p_start:.2f},{p_end:.2f}),iw/{zf:.3f},iw)':"
        f"h='if(between(t,{p_start:.2f},{p_end:.2f}),ih/{zf:.3f},ih)':"
        f"x='(iw-ow)/2':y='(ih-oh)/2',"
        f"scale=1080:1920:flags=lanczos"
    )


def compress_internal_dead_air(
    words: List[Any],
    rough_path: str,
    output_path: str,
    min_gap_sec: float = 0.40,
    target_gap_sec: float = 0.15,
    min_clip_length: float = 0.0,
    use_cpu: bool = False,
    job_id: Optional[str] = None,
) -> Tuple[List[Any], str]:
    """Compress silence gaps > min_gap_sec down to target_gap_sec in audio and video.

    Returns (retimed_words, compressed_video_path).
    Retimes all word subtitle timestamps to match the shortened video track.
    If no gap exceeds min_gap_sec or if trimming would drop the clip below min_clip_length,
    returns the original words and rough_path unchanged.
    """
    if not words or len(words) < 2 or not os.path.isfile(rough_path):
        return words, rough_path

    # Normalize words to dicts
    parsed_words = []
    is_tuple = False
    for w in words:
        if isinstance(w, (list, tuple)):
            is_tuple = True
            parsed_words.append({"start": float(w[0]), "end": float(w[1]), "word": str(w[2])})
        elif isinstance(w, dict):
            parsed_words.append({
                "start": float(w.get("start", 0.0)),
                "end": float(w.get("end", 0.0)),
                "word": str(w.get("word", "")),
            })

    # Find dead air gaps
    gaps = []
    for i in range(len(parsed_words) - 1):
        w_curr = parsed_words[i]
        w_next = parsed_words[i + 1]
        gap_dur = w_next["start"] - w_curr["end"]
        if gap_dur > min_gap_sec:
            excess = gap_dur - target_gap_sec
            cut_start = w_curr["end"] + (target_gap_sec / 2.0)
            cut_end = w_next["start"] - (target_gap_sec / 2.0)
            gaps.append((cut_start, cut_end, excess, i + 1))

    if not gaps:
        return words, rough_path

    total_dur = get_media_duration_seconds(rough_path)
    if total_dur <= 0:
        total_dur = parsed_words[-1]["end"] + 1.0

    # Build kept segments
    segments = []
    curr_t = 0.0
    for cut_s, cut_e, excess, next_w_idx in gaps:
        if cut_s > curr_t:
            segments.append((curr_t, cut_s))
        curr_t = max(curr_t, cut_e)
    if curr_t < total_dur:
        segments.append((curr_t, total_dur))

    if len(segments) <= 1:
        return words, rough_path

    post_dur = sum(seg_e - seg_s for seg_s, seg_e in segments)
    if min_clip_length > 0.0 and post_dur < min_clip_length:
        logger.info(f"Dead-air trimming would reduce clip duration to {post_dur:.1f}s (< {min_clip_length:.1f}s minimum). Skipping dead-air compression.")
        return words, rough_path

    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    filter_parts = []
    inputs_v = []
    inputs_a = []
    for idx, (seg_s, seg_e) in enumerate(segments):
        filter_parts.append(
            f"[0:v]trim=start={seg_s:.3f}:end={seg_e:.3f},setpts=PTS-STARTPTS[v{idx}];"
            f"[0:a]atrim=start={seg_s:.3f}:end={seg_e:.3f},asetpts=PTS-STARTPTS[a{idx}]"
        )
        inputs_v.append(f"[v{idx}]")
        inputs_a.append(f"[a{idx}]")

    concat_inputs = "".join(f"{inputs_v[i]}{inputs_a[i]}" for i in range(len(segments)))
    concat_filter = f"{';'.join(filter_parts)};{concat_inputs}concat=n={len(segments)}:v=1:a=1[outv][outa]"

    enc = get_video_encoder_flags(use_cpu=use_cpu, intermediate=True)
    cmd = [
        ffmpeg_bin, "-y", "-v", "error",
        "-i", os.path.abspath(rough_path),
        "-filter_complex", concat_filter,
        "-map", "[outv]", "-map", "[outa]",
    ] + enc + ["-c:a", "aac", os.path.abspath(output_path)]

    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except Exception as e:
        logger.warning(f"Dead air trimming failed, falling back to original: {e}")
        return words, rough_path

    # Retime words by subtracting cumulative cut duration before each word
    retimed_parsed = []
    for idx, w in enumerate(parsed_words):
        cum_cut = sum(excess for cut_s, cut_e, excess, next_idx in gaps if idx >= next_idx)
        new_start = round(max(0.0, w["start"] - cum_cut), 3)
        new_end = round(max(new_start + 0.05, w["end"] - cum_cut), 3)
        retimed_parsed.append({"start": new_start, "end": new_end, "word": w["word"]})

    if is_tuple:
        retimed_words = [(w["start"], w["end"], w["word"]) for w in retimed_parsed]
    else:
        retimed_words = retimed_parsed

    logger.info(f"Compressed {len(gaps)} dead-air gap(s), saved {sum(g[2] for g in gaps):.2f}s of silence.")
    return retimed_words, output_path


def render_vertical_clip(
    rough_clip_path: str,
    ass_path: Optional[str],
    output_path: str,
    overlay: Optional[dict] = None,
    use_cpu: bool = False,
    job_id: Optional[str] = None,
    layout: str = "auto",
    facecam_info: Optional[Dict[str, Any]] = None,
    progress_cb: Optional[Any] = None,
) -> str:
    """Render 1080x1920 vertical video with facecam-aware layout, burned karaoke captions, and overlay.

    Layout options:
    - 'auto': Automatically select 'split' if facecam is found, else 'gameplay'
    - 'split': Facecam at top (1080x640), gameplay at bottom (1080x1280)
    - 'gameplay': 16:9 gameplay fit in center with blurred, darkened background fill
    - 'face': Reaction crop centered on streamer facecam scaled to full 9:16
    """
    check_cancelled_or_paused(job_id)
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    abs_rough = os.path.abspath(rough_clip_path)
    abs_out = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_out), exist_ok=True)

    cwd_dir = os.path.dirname(abs_rough)
    encoder_flags = get_video_encoder_flags(use_cpu)

    # Determine layout and facecam box
    active_layout = (layout or "auto").lower()
    if active_layout == "auto":
        if facecam_info is None:
            facecam_info = detect_facecam(abs_rough)
        active_layout = facecam_info.get("layout") or ("split" if facecam_info.get("found") else "gameplay")
    elif active_layout in ("split", "duo", "face", "irl") and facecam_info is None:
        facecam_info = detect_facecam(abs_rough)

    frame_size = facecam_info.get("frame_size", (1920, 1080)) if facecam_info else (1920, 1080)
    src_w, src_h = frame_size

    # Build base video filter complex according to layout
    filter_parts: List[str] = []

    L = "flags=lanczos"
    # Blurred, darkened copy of the clip filling the whole 9:16 frame (behind everything)
    BG = ("scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
          "gblur=sigma=12,eq=brightness=-0.18:saturation=1.15,"
          f"scale=1080:1920:{L},setsar=1")

    cams = list((facecam_info or {}).get("cams") or [])
    if active_layout == "duo" and len(cams) < 2:
        active_layout = "split"

    # Extra inputs (AI-sharpened cam pictures) go after the clip itself
    extra_inputs: List[str] = []
    ai_on = upscale_enabled()
    # Cam pictures are shown whole and enlarged at most this much (more looks soft/chopped)
    cam_max_scale = _cam_max_scale()
    pol_cfg = load_config().get("polish", {})
    full_bleed = bool(pol_cfg.get("cam_full_bleed", True))

    def cam_tile(src_label: str, bg_label: str, rect: Tuple[int, int, int, int], cell_w: int,
                 band_h: int, max_h: int, tag: str, out: str) -> Tuple[List[str], str, int, int]:
        """Filters that make one cam tile: full-bleed crop filled edge-to-edge with real pixels,
        or capped-zoom whole cam over a seamless unblurred context crop.
        Returns (filters, "ai"|"src", tile_w, tile_h)."""
        x, y, w, h = rect
        aspect_cell = float(cell_w) / float(max(1, band_h))

        # Center of face / cam
        fb = (facecam_info or {}).get("face_box")
        if fb and (x <= fb[0] + fb[2] / 2.0 <= x + w):
            fcx = fb[0] + fb[2] / 2.0
            fcy = fb[1] + fb[3] / 2.0
        else:
            fcx = x + w / 2.0
            fcy = y + h / 2.0

        if full_bleed:
            # Full-bleed framing: crop matching aspect_cell centered on the face to fill the cell edge-to-edge
            cw = min(src_w, max(w, int(round(h * aspect_cell))))
            ch = min(src_h, max(h, int(round(cw / aspect_cell))))
            cw -= cw % 2
            ch -= ch % 2
            cx = max(0, min(src_w - cw, int(round(fcx - cw / 2.0))))
            cy = max(0, min(src_h - ch, int(round(fcy - ch * 0.45))))
            cx -= cx % 2
            cy -= cy % 2

            fg = None
            if ai_on and needs_upscale(cw, ch, cell_w, band_h):
                up = upscale_crop_video(abs_rough, (cx, cy, cw, ch), cell_w, band_h,
                                        os.path.splitext(abs_out)[0] + f"_{tag}_ai.mp4", job_id=job_id)
                if up:
                    extra_inputs.extend(["-i", up])
                    fg = f"[{len(extra_inputs) // 2}:v] setsar=1 [{out}]"
            parts = []
            if fg is None:
                parts.append(f"[{src_label}] crop={cw}:{ch}:{cx}:{cy},scale={cell_w}:{band_h}:{L},setsar=1 [{out}]")
            else:
                parts.append(fg)
            return parts, ("ai" if fg else "src"), cell_w, band_h
        else:
            # Boxed mode with capped zoom: whole cam centered over unblurred context crop
            tw, th, _ = cam_tile_geometry(rect, cell_w, max_h, cam_max_scale)
            bg_w = min(src_w, max(w, int(round(h * aspect_cell))))
            bg_h = min(src_h, max(h, int(round(bg_w / aspect_cell))))
            bg_x = max(0, min(src_w - bg_w, x - (bg_w - w) // 2))
            bg_y = max(0, min(src_h - bg_h, y - (bg_h - h) // 2))

            parts = [
                f"[{bg_label}] crop={bg_w}:{bg_h}:{bg_x}:{bg_y},scale={cell_w}:{band_h}:{L},setsar=1 [{out}_bg]"
            ]
            fg = None
            if ai_on and needs_upscale(w, h, tw, th):
                up = upscale_crop_video(abs_rough, rect, tw, th,
                                        os.path.splitext(abs_out)[0] + f"_{tag}_ai.mp4", job_id=job_id)
                if up:
                    extra_inputs.extend(["-i", up])
                    fg = f"[{len(extra_inputs) // 2}:v] setsar=1 [{out}_fg]"
            if fg is None:
                parts.append(f"[{src_label}] crop={w}:{h}:{x}:{y},scale={tw}:{th}:{L},setsar=1 [{out}_fg]")
            else:
                parts.append(fg)
            parts.append(f"[{out}_bg][{out}_fg] overlay=(W-w)/2:(H-h)/2 [{out}]")
            return parts, ("ai" if fg else "src"), tw, th

    TOP = TOP_BAND
    if active_layout == "split":
        # Facecam + gameplay: the whole cam (full-bleed or capped zoom) on top, the game below it, hook in the
        # top band and captions in the bottom band so neither covers the cam or the game.
        cam = cams[0] if cams else None
        rect = cam_region(facecam_info, cam, src_w, src_h)
        cam_h = plan_cam_band("split", facecam_info, cam_max_scale)
        game_h = 1920 - TOP - cam_h
        gx, gy, gw, gh = gameplay_crop_avoiding(src_w, src_h, [rect], 1080.0 / game_h)
        tile, kind, tw, th = cam_tile("c0", "c1", rect, 1080, cam_h, 800, "cam", "camt")
        uses = ["[s_bg]", "[s_game]"] + ([] if kind == "ai" else ["[c0]"]) + (["[c1]"] if not full_bleed else [])
        filter_parts.append(
            f"[0:v] split={len(uses)} {''.join(uses)}; "
            f"[s_bg] {BG} [bg]; " + "; ".join(tile) + "; "
            f"[s_game] crop={gw}:{gh}:{gx}:{gy},scale=1080:{game_h}:force_original_aspect_ratio=increase:{L},crop=1080:{game_h},setsar=1 [bot_game]; "
            f"[bg][camt] overlay=0:{TOP} [b1]; [b1][bot_game] overlay=0:{TOP + cam_h} [v_base]"
        )
        logger.info(f"Split: cam {rect} shown at {tw}x{th} (x{tw / max(1, rect[2]):.2f}), game {1080}x{game_h}")
    elif active_layout == "duo":
        # Two streamers (co-op): both whole cams side by side (540 wide each), game below.
        a, b = sorted(cams[:2], key=lambda c: c["rect"][0])
        ra, rb = tuple(a["rect"]), tuple(b["rect"])
        cam_h = plan_cam_band("duo", facecam_info, cam_max_scale)
        game_h = 1920 - TOP - cam_h
        gx, gy, gw, gh = gameplay_crop_avoiding(src_w, src_h, [ra, rb], 1080.0 / game_h)
        ta, ka, taw, tah = cam_tile("a0", "a1", ra, 540, cam_h, 700, "cam_a", "cama")
        tb, kb, tbw, tbh = cam_tile("b0", "b1", rb, 540, cam_h, 700, "cam_b", "camb")
        uses = ["[s_bg]", "[s_game]"] + ([] if ka == "ai" else ["[a0]"]) + (["[a1]"] if not full_bleed else []) + ([] if kb == "ai" else ["[b0]"]) + (["[b1]"] if not full_bleed else [])
        filter_parts.append(
            f"[0:v] split={len(uses)} {''.join(uses)}; "
            f"[s_bg] {BG} [bg]; " + "; ".join(ta) + "; " + "; ".join(tb) + "; "
            f"[s_game] crop={gw}:{gh}:{gx}:{gy},scale=1080:{game_h}:force_original_aspect_ratio=increase:{L},crop=1080:{game_h},setsar=1 [bot_game]; "
            f"[bg][cama] overlay=0:{TOP} [m1]; [m1][camb] overlay=540:{TOP} [m2]; "
            f"[m2][bot_game] overlay=0:{TOP + cam_h} [v_base]"
        )
        logger.info(f"Duo: cams shown at {taw}x{tah} and {tbw}x{tbh}, game 1080x{game_h}")
    elif active_layout in ("irl", "face") and (facecam_info or {}).get("track"):
        # Moving 9:16 window that follows the main person (IRL / camera streams)
        crop_h = src_h
        crop_w = min(src_w, int(round(crop_h * 9 / 16 / 2)) * 2)
        x_expr = build_pan_expression(facecam_info["track"], crop_w, src_w)
        filter_parts.append(
            f"[0:v] crop={crop_w}:{crop_h}:x='{x_expr}':y=0,scale=1080:1920:{L},setsar=1 [v_base]"
        )
    elif active_layout in ("face", "irl"):
        face_box = facecam_info.get("face_box") if facecam_info else None
        if face_box:
            fx, fy, fw, fh = face_box
            cx = fx + fw // 2
            cy = fy + fh // 2
        else:
            cx, cy = src_w // 2, src_h // 2

        crop_h = src_h
        crop_w = int(crop_h * 9 / 16)
        if crop_w > src_w:
            crop_w = src_w
            crop_h = int(crop_w * 16 / 9)
        crop_x = max(0, min(src_w - crop_w, cx - crop_w // 2))
        crop_y = max(0, min(src_h - crop_h, cy - crop_h // 2))

        filter_parts.append(
            f"[0:v] crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale=1080:1920:{L},setsar=1 [v_base]"
        )
    else:
        # 'gameplay' = Framed: the whole picture sits in the middle band (no stretching,
        # no zoom-blur), on top of a blurred copy of itself that fills the screen.
        fc_cfg = load_config().get("facecam", {})
        try:
            aw, ah = [float(x) for x in str(fc_cfg.get("gameplay_aspect", "16:9")).split(":")]
            g_aspect = max(9 / 16, min(16 / 9, aw / ah))
        except Exception:
            g_aspect = 16 / 9
        fg_h = int(round(1080 / g_aspect / 2) * 2)
        filter_parts.append(
            "[0:v] split [main_fg][main_bg]; "
            f"[main_bg] {BG} [bg_blur]; "
            f"[main_fg] crop='min(iw,ih*{g_aspect:.4f})':ih,scale=1080:{fg_h}:{L},setsar=1 [fg_game]; "
            "[bg_blur][fg_game] overlay=(W-w)/2:(H-h)/2 [v_base]"
        )

    # Gentle clean-up: light denoise + light sharpen (never harsh)
    enh = str(load_config().get("polish", {}).get("enhance", "light")).lower()
    if enh in ("light", "on", "true"):
        filter_parts.append("[v_base] hqdn3d=1.2:1.2:3:3,unsharp=5:5:0.55:5:5:0.0 [v_enh]")
        base_label = "[v_enh]"
    else:
        base_label = "[v_base]"

    curr_v = base_label

    # Subtle punch-zoom (1.10x - 1.15x) on punchline climax if enabled
    pz_cfg = load_config().get("polish", {}).get("punch_zoom", {})
    if pz_cfg.get("enabled", True):
        dur = get_media_duration_seconds(abs_rough)
        zf = float(pz_cfg.get("factor", 1.12))
        pz_filt = build_punch_zoom_filter(dur, zoom_factor=zf)
        if pz_filt:
            filter_parts.append(f"{curr_v} {pz_filt} [v_zoom]")
            curr_v = "[v_zoom]"

    # Burn in ASS captions if present
    if ass_path and os.path.isfile(ass_path):
        abs_ass = os.path.abspath(ass_path)
        if os.path.dirname(abs_ass) == cwd_dir:
            rel_ass = os.path.basename(abs_ass)
            ass_filter = f"ass={rel_ass}"
        else:
            escaped = escape_ffmpeg_ass_path(abs_ass)
            ass_filter = f"ass='{escaped}'"
        filter_parts.append(f"{curr_v} {ass_filter} [v_sub]")
        curr_v = "[v_sub]"

    inputs = ["-i", abs_rough] + extra_inputs

    def n_inputs() -> int:
        return inputs.count("-i")

    # Emoji reactions: little pictures shown above the caption while the matching words play
    for k, ev in enumerate(_read_emoji_events(ass_path)):
        em_idx = n_inputs()
        inputs.extend(["-loop", "1", "-i", ev["path"]])
        filter_parts.append(
            f"[{em_idx}:v] format=rgba,scale=150:150 [emo{k}]; "
            f"{curr_v}[emo{k}] overlay=x=(W-w)/2:y={int(ev['y'])}:"
            f"enable='between(t,{ev['start']:.2f},{ev['end']:.2f})':shortest=1 [emv{k}]"
        )
        curr_v = f"[emv{k}]"

    # Burn in overlay image if present
    overlay_img = overlay.get("image_path") if overlay else None
    has_overlay = overlay_img and os.path.isfile(overlay_img)

    if has_overlay:
        abs_overlay = os.path.abspath(overlay_img)
        ov_idx = n_inputs()
        inputs.extend(["-i", abs_overlay])
        ox = str(overlay.get("x", "(W-w)/2"))
        oy = str(overlay.get("y", "120"))
        ow = str(overlay.get("width", -1))
        oh = str(overlay.get("height", -1))
        over_scale = f"scale={ow}:{oh}" if (ow != "-1" or oh != "-1") else "null"
        filter_parts.append(f"[{ov_idx}:v] {over_scale} [v_over]; {curr_v}[v_over] overlay={ox}:{oy} [outv]")
        curr_v = "[outv]"

    # Progress bar along the bottom edge (fills as the clip plays)
    pol = load_config().get("polish", {})
    if pol.get("progress_bar", True):
        try:
            clip_dur = float(get_media_duration_seconds(abs_rough))
        except Exception:
            clip_dur = 0.0
        if clip_dur > 1.0:
            bar_color = str(pol.get("progress_bar_color", "yellow"))
            filter_parts.append(
                f"color=c={bar_color}:s=1080x10:r=30 [pbar]; "
                f"{curr_v}[pbar] overlay=x='-W+W*t/{clip_dur:.3f}':y=H-10:shortest=1 [v_bar]"
            )
            curr_v = "[v_bar]"

    full_filter_complex = "; ".join(filter_parts)

    # Optional two-pass loudness normalization
    cfg = load_config()
    polish_cfg = cfg.get("polish", {}).get("loudnorm", {})
    af_filter = None
    if polish_cfg.get("enabled", True):
        af_filter = get_loudnorm_audio_filter(
            abs_rough,
            target_lufs=polish_cfg.get("target_lufs", -14.0),
            target_tp=polish_cfg.get("target_tp", -1.0),
            target_lra=polish_cfg.get("target_lra", 11.0),
        )

    # Short fades so the clip doesn't start/stop with a click mid-word
    try:
        a_dur = float(get_media_duration_seconds(abs_rough))
    except Exception:
        a_dur = 0.0
    a_chain = [af_filter] if af_filter else []
    if a_dur > 2.0:
        a_chain += ["afade=t=in:st=0:d=0.12", f"afade=t=out:st={max(0.0, a_dur - 0.35):.3f}:d=0.35"]
    audio_flags = ["-c:a", "aac", "-b:a", "160k"]
    if a_chain:
        audio_flags = ["-af", ",".join(a_chain), *audio_flags]

    cmd = [
        ffmpeg_bin, "-y", "-progress", "pipe:1", "-nostats",
        *inputs,
        "-filter_complex", full_filter_complex,
        "-map", curr_v,
        "-map", "0:a",
        "-pix_fmt", "yuv420p",
        *encoder_flags,
        *audio_flags,
        abs_out
    ]

    logger.info(f"Rendering 9:16 vertical clip (layout={active_layout}) -> {output_path}")

    def on_line(line: str):
        # ffmpeg -progress prints out_time_us=<microseconds> as it encodes
        if progress_cb and a_dur > 0 and line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                progress_cb(min(1.0, int(line.split("=", 1)[1]) / 1e6 / a_dur))
            except ValueError:
                pass

    try:
        ret = run_tracked_command(cmd, job_id=job_id, cwd=cwd_dir, line_callback=on_line)
        if ret != 0:
            raise RuntimeError(f"FFmpeg vertical render exited with code {ret}")
    except Exception as e:
        check_cancelled_or_paused(job_id)
        if not use_cpu:
            logger.warning(
                f"NVENC vertical render failed ({e}). Falling back to libx264..."
            )
            return render_vertical_clip(
                rough_clip_path,
                ass_path,
                output_path,
                overlay=overlay,
                use_cpu=True,
                job_id=job_id,
                layout=layout,
                facecam_info=facecam_info,
                progress_cb=progress_cb,
            )
        raise RuntimeError(f"FFmpeg vertical render failed: {e}")

    # Generate high quality poster thumbnail for instant web previews without video buffering
    poster_path = os.path.splitext(abs_out)[0] + ".jpg"
    try:
        poster_time = min(1.0, max(0.1, a_dur * 0.2)) if a_dur > 0 else 0.5
        subprocess.run(
            [ffmpeg_bin, "-y", "-ss", f"{poster_time:.2f}", "-i", abs_out, "-vframes", "1", "-q:v", "3", poster_path],
            capture_output=True, timeout=10
        )
    except Exception:
        pass

    check_cancelled_or_paused(job_id)
    return abs_out
