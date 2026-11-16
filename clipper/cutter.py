"""Video cutting and vertical 9:16 rendering with tracked processes and NVENC."""
import logging
import os
from typing import List, Optional

from .captions import escape_ffmpeg_ass_path
from .process_manager import check_cancelled_or_paused, run_tracked_command
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.cutter")


def get_video_encoder_flags(use_cpu: bool = False) -> List[str]:
    """Return FFmpeg video encoder parameters for NVENC or libx264 CPU fallback."""
    if use_cpu:
        return ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
    return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21"]


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


def render_vertical_clip(
    rough_clip_path: str,
    ass_path: Optional[str],
    output_path: str,
    overlay: Optional[dict] = None,
    use_cpu: bool = False,
    job_id: Optional[str] = None,
) -> str:
    """Render 1080x1920 vertical video with burned-in karaoke captions and optional campaign overlay."""
    check_cancelled_or_paused(job_id)
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    abs_rough = os.path.abspath(rough_clip_path)
    abs_out = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_out), exist_ok=True)

    cwd_dir = os.path.dirname(abs_rough)
    encoder_flags = get_video_encoder_flags(use_cpu)

    base_filters = ["crop=ih*9/16:ih", "scale=1080:1920", "setsar=1"]

    if ass_path and os.path.isfile(ass_path):
        abs_ass = os.path.abspath(ass_path)
        if os.path.dirname(abs_ass) == cwd_dir:
            rel_ass = os.path.basename(abs_ass)
            base_filters.append(f"ass={rel_ass}")
        else:
            escaped = escape_ffmpeg_ass_path(abs_ass)
            base_filters.append(f"ass='{escaped}'")

    overlay_img = overlay.get("image_path") if overlay else None
    has_overlay = overlay_img and os.path.isfile(overlay_img)

    if has_overlay:
        abs_overlay = os.path.abspath(overlay_img)
        ox = str(overlay.get("x", "(W-w)/2"))
        oy = str(overlay.get("y", "120"))
        ow = str(overlay.get("width", -1))
        oh = str(overlay.get("height", -1))

        v0_chain = ",".join(base_filters)
        over_scale = f"scale={ow}:{oh}" if (ow != "-1" or oh != "-1") else "null"
        filter_complex = (
            f"[0:v]{v0_chain}[v_sub];"
            f"[1:v]{over_scale}[v_over];"
            f"[v_sub][v_over]overlay={ox}:{oy}[outv]"
        )

        cmd = [
            ffmpeg_bin, "-y",
            "-i", abs_rough,
            "-i", abs_overlay,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "0:a",
            "-pix_fmt", "yuv420p",
            *encoder_flags,
            "-c:a", "aac",
            "-b:a", "160k",
            abs_out
        ]
    else:
        vf_string = ",".join(base_filters)
        cmd = [
            ffmpeg_bin, "-y",
            "-i", abs_rough,
            "-vf", vf_string,
            "-pix_fmt", "yuv420p",
            *encoder_flags,
            "-c:a", "aac",
            "-b:a", "160k",
            abs_out
        ]

    logger.info(f"Rendering 9:16 vertical clip with burned captions -> {output_path}")

    try:
        ret = run_tracked_command(cmd, job_id=job_id, cwd=cwd_dir)
        if ret != 0:
            raise RuntimeError(f"FFmpeg vertical render exited with code {ret}")
    except Exception as e:
        check_cancelled_or_paused(job_id)
        if not use_cpu:
            logger.warning(
                f"NVENC vertical render failed ({e}). Falling back to libx264..."
            )
            return render_vertical_clip(
                rough_clip_path, ass_path, output_path, overlay=overlay, use_cpu=True, job_id=job_id
            )
        raise RuntimeError(f"FFmpeg vertical render failed: {e}")

    check_cancelled_or_paused(job_id)
    return abs_out
