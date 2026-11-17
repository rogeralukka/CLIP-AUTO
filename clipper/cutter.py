"""Video cutting and vertical 9:16 rendering with tracked processes and NVENC."""
import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

import numpy as np

from .captions import escape_ffmpeg_ass_path
from .config import load_config
from .facecam import compute_gameplay_crop, detect_facecam
from .process_manager import check_cancelled_or_paused, run_tracked_command
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.cutter")


def get_video_encoder_flags(use_cpu: bool = False) -> List[str]:
    """Return FFmpeg video encoder parameters for NVENC or libx264 CPU fallback."""
    if use_cpu:
        return ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
    return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21"]


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
    from .detection import get_media_duration_seconds
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

    encoder_flags = get_video_encoder_flags(use_cpu)

    cmd = [
        ffmpeg_bin, "-y",
        "-ss", str(round(start_sec, 3)),
        "-i", os.path.abspath(source_video),
        "-t", str(round(duration, 3)),
        *encoder_flags,
        "-c:a", "aac",
        "-b:a", "160k",
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
    elif active_layout in ("split", "face", "irl") and facecam_info is None:
        facecam_info = detect_facecam(abs_rough)

    frame_size = facecam_info.get("frame_size", (1920, 1080)) if facecam_info else (1920, 1080)
    src_w, src_h = frame_size

    # Build base video filter complex according to layout
    filter_parts: List[str] = []

    if active_layout == "split":
        padded_box = facecam_info.get("padded_box") if facecam_info else None
        corner = facecam_info.get("corner", "top_right") if facecam_info else "top_right"

        if padded_box:
            fx, fy, fw, fh = padded_box
        else:
            # Sane default top-right corner
            fw, fh = int(src_w * 0.28), int(src_h * 0.35)
            fx, fy = max(0, src_w - fw), 0

        gx, gy, gw, gh = compute_gameplay_crop(src_w, src_h, corner)

        filter_parts.append(
            f"[0:v] crop={fw}:{fh}:{fx}:{fy},scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640,setsar=1 [top_face]; "
            f"[0:v] crop={gw}:{gh}:{gx}:{gy},scale=1080:1280:force_original_aspect_ratio=increase,crop=1080:1280,setsar=1 [bot_game]; "
            f"[top_face][bot_game] vstack=inputs=2 [v_base]"
        )
    elif active_layout in ("irl", "face") and (facecam_info or {}).get("track"):
        # Moving 9:16 window that follows the main person (IRL / camera streams)
        crop_h = src_h
        crop_w = min(src_w, int(round(crop_h * 9 / 16 / 2)) * 2)
        x_expr = build_pan_expression(facecam_info["track"], crop_w, src_w)
        filter_parts.append(
            f"[0:v] crop={crop_w}:{crop_h}:x='{x_expr}':y=0,scale=1080:1920,setsar=1 [v_base]"
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
            f"[0:v] crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale=1080:1920,setsar=1 [v_base]"
        )
    else:
        # Default 'gameplay': 16:9 gameplay fit in center with blurred background
        # Center-crop the gameplay a little (default 4:3) so it is bigger on a phone screen
        fc_cfg = load_config().get("facecam", {})
        try:
            aw, ah = [float(x) for x in str(fc_cfg.get("gameplay_aspect", "4:3")).split(":")]
            g_aspect = max(9 / 16, min(16 / 9, aw / ah))
        except Exception:
            g_aspect = 4 / 3
        fg_h = int(round(1080 / g_aspect / 2) * 2)
        filter_parts.append(
            "[0:v] split [main_fg][main_bg]; "
            "[main_bg] scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5,eq=brightness=-0.15 [bg_blur]; "
            f"[main_fg] crop='min(iw,ih*{g_aspect:.4f})':ih,scale=1080:{fg_h},setsar=1 [fg_game]; "
            "[bg_blur][fg_game] overlay=(W-w)/2:(H-h)/2 [v_base]"
        )

    curr_v = "[v_base]"

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

    # Burn in overlay image if present
    overlay_img = overlay.get("image_path") if overlay else None
    has_overlay = overlay_img and os.path.isfile(overlay_img)

    inputs = ["-i", abs_rough]
    if has_overlay:
        abs_overlay = os.path.abspath(overlay_img)
        inputs.extend(["-i", abs_overlay])
        ox = str(overlay.get("x", "(W-w)/2"))
        oy = str(overlay.get("y", "120"))
        ow = str(overlay.get("width", -1))
        oh = str(overlay.get("height", -1))
        over_scale = f"scale={ow}:{oh}" if (ow != "-1" or oh != "-1") else "null"
        filter_parts.append(f"[1:v] {over_scale} [v_over]; {curr_v}[v_over] overlay={ox}:{oy} [outv]")
        curr_v = "[outv]"

    # Progress bar along the bottom edge (fills as the clip plays)
    pol = load_config().get("polish", {})
    if pol.get("progress_bar", True):
        try:
            from .detection import get_media_duration_seconds
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
        from .detection import get_media_duration_seconds
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

    check_cancelled_or_paused(job_id)
    return abs_out
