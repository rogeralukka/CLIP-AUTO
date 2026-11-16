"""End-to-end clipper pipeline orchestrator with stage-based progress, cancellation, pausing, and quality selection."""
import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .captions import load_preset, make_ass
from .cutter import cut_rough_clip, render_vertical_clip
from .detection import find_moments, spike_moments
from .downloader import prepare_input
from .process_manager import check_cancelled_or_paused, is_job_paused, log_job_message
from .transcription import transcribe_clip
from .utils import get_ffmpeg_path, setup_cuda_dlls


def setup_logging(log_file: str = "clipper.log"):
    """Configure logger to stream to stdout and append to clipper.log."""
    logger = logging.getLogger("clipper")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        c_handler = logging.StreamHandler(sys.stdout)
        f_handler = logging.FileHandler(log_file, encoding="utf-8")
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        c_handler.setFormatter(formatter)
        f_handler.setFormatter(formatter)
        logger.addHandler(c_handler)
        logger.addHandler(f_handler)
    return logger


logger = setup_logging()


def detect_pipeline_moments(
    vod_input: str,
    chat_input: Optional[str] = None,
    quality: str = "1080p",
    top: Optional[int] = None,
    before: int = 20,
    after: int = 30,
    window: int = 5,
    z_thresh: float = 2.0,
    min_msgs: int = 10,
    merge_gap: int = 15,
    safety_max: int = 50,
    cache_dir: str = "downloads",
    job_id: Optional[str] = None,
    progress_callback: Optional[Callable[[str, int, str, Dict[str, Any]], None]] = None,
) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
    """Stage 1 & 2: Download / resolve video & chat with quality selection, then detect candidate hype moments."""
    check_cancelled_or_paused(job_id)
    setup_cuda_dlls()
    os.makedirs(cache_dir, exist_ok=True)

    log_job_message(job_id, f"Initiating stream preparation for {vod_input} (quality={quality})...")

    # 1. Download / Resolve Video & Chat
    def on_dl_progress(sub_stage: str, info: Dict[str, Any]):
        check_cancelled_or_paused(job_id)
        if not progress_callback:
            return

        pct = info.get("percent", 0.0)
        if sub_stage == "download_video":
            overall_pct = int(min(20, (pct / 100.0) * 20))
            speed = info.get("speed", "")
            size = info.get("size", "")
            eta = info.get("eta", "")
            parts = [f"Downloading video: {pct:.1f}%"]
            if speed:
                parts.append(f"at {speed}")
            if size:
                parts.append(f"({size})")
            if eta:
                parts.append(f"ETA {eta}")
            msg = " ".join(parts) if len(parts) > 1 else (info.get("status") or "Downloading stream video...")
            progress_callback("download", overall_pct, msg, info)
        elif sub_stage == "download_chat":
            msg = info.get("status") or "Downloading chat replay..."
            progress_callback("download", 22, f"Chat: {msg}", info)

    if progress_callback:
        progress_callback("download", 2, "Connecting to stream source...", {})

    video_path, resolved_chat_path = prepare_input(
        vod_input,
        chat_file=chat_input,
        quality=quality,
        output_dir=cache_dir,
        job_id=job_id,
        progress_callback=on_dl_progress,
    )
    check_cancelled_or_paused(job_id)

    logger.info(f"Video source ready: {video_path}")
    logger.info(f"Chat source ready : {resolved_chat_path or 'None (using audio loudness fallback)'}")
    log_job_message(job_id, f"Video ready: {video_path}")

    if progress_callback:
        progress_callback("detect", 25, "Loading chat data & analyzing hype spikes...", {})

    # 2. Load Chat comments if available
    comments = None
    if resolved_chat_path and os.path.isfile(resolved_chat_path):
        try:
            with open(resolved_chat_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                comments = data.get("comments", [])
                logger.info(f"Loaded {len(comments)} chat comments from {resolved_chat_path}")
                log_job_message(job_id, f"Loaded {len(comments)} chat comments.")
        except Exception as e:
            logger.warning(f"Could not load chat JSON ({e}). Falling back to audio loudness.")
            comments = None

    # 3. Detect Hype Moments
    moments = find_moments(
        comments=comments,
        video_path=video_path,
        window=window,
        z_thresh=z_thresh,
        min_msgs=min_msgs,
        before=before,
        after=after,
        merge_gap=merge_gap,
        top=top,
        safety_max=safety_max,
        job_id=job_id,
    )
    check_cancelled_or_paused(job_id)

    log_job_message(job_id, f"Detection complete: found {len(moments)} hype moments.")

    if progress_callback:
        progress_callback(
            "detect",
            30,
            f"Found {len(moments)} hype moments (z-thresh >= {z_thresh:.1f}).",
            {"count": len(moments)},
        )

    return video_path, resolved_chat_path, moments


def render_pipeline_moments(
    video_path: str,
    moments: List[Dict[str, Any]],
    output_dir: str = "clips",
    preset_name: str = "classic_yellow",
    model: str = "large-v3",
    cpu: bool = False,
    overlay: Optional[dict] = None,
    job_id: Optional[str] = None,
    progress_callback: Optional[Callable[[str, int, str, Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """Stage 4 & 5: Cut, transcribe with faster-whisper, and render vertical 9:16 shorts with instant resumption."""
    check_cancelled_or_paused(job_id)
    setup_cuda_dlls()
    os.makedirs(output_dir, exist_ok=True)

    preset = load_preset(preset_name)
    logger.info(f"Rendering {len(moments)} moments with caption preset '{preset.get('name', preset_name)}'...")
    log_job_message(job_id, f"Beginning render loop for {len(moments)} clips...")

    generated_clips: List[Dict[str, Any]] = []
    total = len(moments)
    if total == 0:
        return []

    for i, m in enumerate(moments, 1):
        check_cancelled_or_paused(job_id)
        if isinstance(m, dict):
            start_sec = m["start"]
            end_sec = m["end"]
            score = m.get("score", 0.0)
            preview = m.get("preview", "")
        else:
            start_sec, end_sec, score = m[:3]
            preview = ""

        clip_base = f"clip_{i:02d}"
        rough_path = os.path.abspath(os.path.join(output_dir, f"rough_{i:02d}.mp4"))
        ass_path = os.path.abspath(os.path.join(output_dir, f"cap_{i:02d}.ass"))
        final_path = os.path.abspath(os.path.join(output_dir, f"{clip_base}.mp4"))

        # Check if already rendered from a previous paused run (Requirement 1: Resumption)
        if os.path.isfile(final_path) and os.path.getsize(final_path) > 1024:
            logger.info(f"Clip {clip_base} already rendered; skipping to next clip.")
            log_job_message(job_id, f"Clip {clip_base} already completed, skipping.")
            transcript_file = os.path.join(output_dir, f"rough_{i:02d}.transcript.json")
            transcript_text = ""
            word_count = 0
            if os.path.isfile(transcript_file):
                try:
                    with open(transcript_file, "r", encoding="utf-8") as tf:
                        w_data = json.load(tf)
                        t_words = w_data.get("words", [])
                        transcript_text = " ".join(w.get("word", "") for w in t_words)
                        word_count = len(t_words)
                except Exception:
                    pass

            generated_clips.append({
                "index": i,
                "filename": os.path.basename(final_path),
                "path": final_path,
                "start_seconds": start_sec,
                "end_seconds": end_sec,
                "duration": end_sec - start_sec,
                "hype_score": score,
                "preview": preview,
                "word_count": word_count,
                "transcript": transcript_text,
            })
            continue

        # Progress calculation: 35% -> 95%
        step_base_pct = 35 + int(((i - 1) / total) * 60)

        # Step A: Cutting
        if progress_callback:
            progress_callback(
                "render",
                step_base_pct,
                f"Cutting rough clip: clip {i} of {total} ({start_sec}s -> {end_sec}s)",
                {"clip_index": i, "total": total},
            )

        if not (os.path.isfile(rough_path) and os.path.getsize(rough_path) > 1024):
            cut_rough_clip(video_path, start_sec, end_sec, rough_path, use_cpu=cpu, job_id=job_id)
        check_cancelled_or_paused(job_id)

        # Step B: Transcribe
        if progress_callback:
            progress_callback(
                "transcribe",
                step_base_pct + int(20 / total),
                f"Transcribing: clip {i} of {total} (Whisper {model})",
                {"clip_index": i, "total": total},
            )

        words = transcribe_clip(
            rough_path,
            model_name=model,
            use_cpu=cpu,
            cache_dir=output_dir,
            job_id=job_id,
        )
        check_cancelled_or_paused(job_id)

        # Step C: Subtitles
        make_ass(words, ass_path, preset=preset)

        # Step D: Render Vertical 9:16
        if progress_callback:
            progress_callback(
                "render",
                step_base_pct + int(40 / total),
                f"Rendering: clip {i} of {total} (1080x1920 NVENC)",
                {"clip_index": i, "total": total},
            )

        render_vertical_clip(
            rough_path,
            ass_path,
            final_path,
            overlay=overlay,
            use_cpu=cpu,
            job_id=job_id,
        )
        check_cancelled_or_paused(job_id)

        transcript_text = " ".join(w[2] for w in words)
        generated_clips.append({
            "index": i,
            "filename": os.path.basename(final_path),
            "path": final_path,
            "start_seconds": start_sec,
            "end_seconds": end_sec,
            "duration": end_sec - start_sec,
            "hype_score": score,
            "preview": preview,
            "word_count": len(words),
            "transcript": transcript_text,
        })
        log_job_message(job_id, f"Finished rendering clip {i} of {total} ({os.path.basename(final_path)})")

        if progress_callback:
            progress_callback(
                "render",
                35 + int((i / total) * 60),
                f"Finished clip {i} of {total}",
                {"clip_index": i, "total": total},
            )

    return generated_clips


def run_pipeline(
    vod_input: str,
    chat_input: Optional[str] = None,
    quality: str = "1080p",
    top: Optional[int] = 8,
    before: int = 20,
    after: int = 30,
    window: int = 5,
    z_thresh: float = 2.0,
    min_msgs: int = 10,
    merge_gap: int = 15,
    safety_max: int = 50,
    model: str = "large-v3",
    cpu: bool = False,
    preset_name: str = "classic_yellow",
    output_dir: str = "clips",
    cache_dir: str = "downloads",
    overlay: Optional[dict] = None,
    job_id: Optional[str] = None,
    moments: Optional[List[Dict[str, Any]]] = None,
    detect_only: bool = False,
    progress_callback: Optional[Callable[[str, int, str, Dict[str, Any]], None]] = None,
) -> Any:
    """Execute the stream clipper pipeline end-to-end or staged."""
    check_cancelled_or_paused(job_id)

    # If moments already provided, jump straight to rendering
    if moments is not None:
        return render_pipeline_moments(
            video_path=vod_input,
            moments=moments,
            output_dir=output_dir,
            preset_name=preset_name,
            model=model,
            cpu=cpu,
            overlay=overlay,
            job_id=job_id,
            progress_callback=progress_callback,
        )

    # Otherwise detect moments
    video_path, chat_path, detected_moments = detect_pipeline_moments(
        vod_input=vod_input,
        chat_input=chat_input,
        quality=quality,
        top=top,
        before=before,
        after=after,
        window=window,
        z_thresh=z_thresh,
        min_msgs=min_msgs,
        merge_gap=merge_gap,
        safety_max=safety_max,
        cache_dir=cache_dir,
        job_id=job_id,
        progress_callback=progress_callback,
    )

    if detect_only:
        return detected_moments

    if not detected_moments:
        logger.warning("No hype moments detected.")
        return []

    return render_pipeline_moments(
        video_path=video_path,
        moments=detected_moments,
        output_dir=output_dir,
        preset_name=preset_name,
        model=model,
        cpu=cpu,
        overlay=overlay,
        job_id=job_id,
        progress_callback=progress_callback,
    )
