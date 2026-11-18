"""Optional AI sharpening of small cam crops with Real-ESRGAN (free, open source, runs on the GPU).

A facecam is a small part of the stream frame; in the split / two-cam layouts it gets
blown up 1.5-3x, which is where it goes soft. Real-ESRGAN (ncnn-vulkan build, works on
any NVIDIA/AMD/Intel GPU) redraws those few pixels sharper. Only the cam crop goes
through it - the game half is already at (or above) its native size.

Install once with setup_upscaler.ps1 (downloads the official release from GitHub into
bin/realesrgan). Without it, or if anything fails, clips render exactly as before.
"""
import glob
import logging
import os
import shutil
import subprocess
from typing import Optional, Tuple

from .config import load_config
from .process_manager import run_tracked_command
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.upscale")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPSCALER_DIR = os.path.join(BASE_DIR, "bin", "realesrgan")


def find_upscaler() -> Optional[str]:
    for name in ("realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan"):
        p = os.path.join(UPSCALER_DIR, name)
        if os.path.isfile(p):
            return p
    return shutil.which("realesrgan-ncnn-vulkan")


def upscale_enabled() -> bool:
    mode = str(load_config().get("polish", {}).get("ai_upscale_cams", "auto")).lower()
    if mode in ("off", "false", "no", "0"):
        return False
    return find_upscaler() is not None


def needs_upscale(crop_w: int, crop_h: int, out_w: int, out_h: int) -> bool:
    """Only worth it when the crop is blown up a lot."""
    factor = max(out_w / max(1, crop_w), out_h / max(1, crop_h))
    return factor >= float(load_config().get("polish", {}).get("ai_upscale_min_factor", 1.15))


def upscale_crop_video(src: str, crop: Tuple[int, int, int, int], out_w: int, out_h: int,
                       out_path: str, job_id: Optional[str] = None) -> Optional[str]:
    """Crop `src` to `crop` (x, y, w, h), AI-upscale every frame, scale to out_w x out_h.

    Returns the path of a silent video with the same frame rate as `src`, or None on any
    failure (the caller then renders the normal way).
    """
    exe = find_upscaler()
    if not exe:
        return None
    ffmpeg = get_ffmpeg_path() or "ffmpeg"
    x, y, w, h = crop
    work = out_path + "_frames"
    fin, fout = os.path.join(work, "in"), os.path.join(work, "out")
    try:
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(fin)
        os.makedirs(fout)
        fps = _frame_rate(src) or "30"
        ret = run_tracked_command([ffmpeg, "-y", "-loglevel", "error", "-i", src,
                                   "-vf", f"crop={w}:{h}:{x}:{y}", "-q:v", "2",
                                   os.path.join(fin, "%06d.jpg")], job_id=job_id)
        n_in = len(glob.glob(os.path.join(fin, "*.jpg")))
        if ret != 0 or n_in == 0:
            return None
        model = str(load_config().get("polish", {}).get("ai_upscale_model", "realesrgan-x4plus"))
        ret = run_tracked_command([exe, "-i", fin, "-o", fout, "-n", model, "-s", "4", "-f", "jpg",
                                   "-m", os.path.join(os.path.dirname(exe), "models")],
                                  job_id=job_id, cwd=os.path.dirname(exe))
        n_out = len(glob.glob(os.path.join(fout, "*.jpg")))
        if ret != 0 or n_out != n_in:
            logger.warning(f"AI upscale failed (exit {ret}, {n_out}/{n_in} frames); rendering without it")
            return None
        ret = run_tracked_command([ffmpeg, "-y", "-loglevel", "error", "-framerate", fps,
                                   "-i", os.path.join(fout, "%06d.jpg"),
                                   "-vf", f"scale={out_w}:{out_h}:flags=lanczos,setsar=1",
                                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
                                   "-pix_fmt", "yuv420p", out_path], job_id=job_id)
        if ret != 0 or not os.path.isfile(out_path):
            return None
        logger.info(f"AI-sharpened cam crop {w}x{h} -> {out_w}x{out_h} ({n_out} frames)")
        return out_path
    except Exception as e:
        logger.warning(f"AI upscale error: {e}; rendering without it")
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _frame_rate(path: str) -> Optional[str]:
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        if fps and fps > 1:
            return f"{fps:.3f}"
    except Exception:
        pass
    try:
        out = subprocess.run([get_ffmpeg_path() or "ffmpeg", "-i", path], capture_output=True, text=True).stderr
        import re
        m = re.search(r"([\d.]+) fps", out)
        return m.group(1) if m else None
    except Exception:
        return None
