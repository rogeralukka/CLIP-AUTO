"""End-to-end clipper pipeline orchestrator with audio-first ingestion and full stream transcription."""
import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .captions import load_preset, make_ass
from .config import load_config
from .cutter import cut_rough_clip, render_vertical_clip
from .detection import find_moments
from .downloader import (
    download_audio_and_chat,
    download_video_section,
    get_vod_id,
    is_url,
    prepare_input,
)
from .process_manager import check_cancelled_or_paused, is_job_paused, log_job_message
from .transcription import (
    get_transcript_text_in_window,
    slice_transcript,
    transcribe_clip,
    transcribe_full_audio,
)
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
    model: Optional[str] = None,
    cpu: bool = False,
    cache_dir: str = "downloads",
    job_id: Optional[str] = None,
    progress_callback: Optional[Callable[[str, int, str, Dict[str, Any]], None]] = None,
) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
    """Phase 1: Download audio-only & chat upfront, transcribe full stream ONCE, and detect hype moments."""
    check_cancelled_or_paused(job_id)
    setup_cuda_dlls()
    os.makedirs(cache_dir, exist_ok=True)
    cfg = load_config()

    log_job_message(job_id, f"Initiating audio-first stream ingestion for {vod_input}...")

    # 1. Download / Resolve Audio & Chat (Audio-Only upfront)
    def on_dl_progress(sub_stage: str, info: Dict[str, Any]):
        check_cancelled_or_paused(job_id)
        if not progress_callback:
            return

        pct = info.get("percent", 0.0)
        if sub_stage == "download_audio":
            overall_pct = int(min(15, (pct / 100.0) * 15))
            speed = info.get("speed", "")
            size = info.get("size", "")
            eta = info.get("eta", "")
            parts = [f"Downloading audio: {pct:.1f}%"]
            if speed:
                parts.append(f"at {speed}")
            if size:
                parts.append(f"({size})")
            if eta:
                parts.append(f"ETA {eta}")
            msg = " ".join(parts) if len(parts) > 1 else (info.get("status") or "Downloading stream audio...")
            progress_callback("download", overall_pct, msg, info)
        elif sub_stage == "download_chat":
            msg = info.get("status") or "Downloading chat replay..."
            progress_callback("download", 18, f"Chat: {msg}", info)

    if progress_callback:
        progress_callback("download", 2, "Connecting to stream source (audio-first)...", {})

    audio_path, resolved_chat_path, vod_id = download_audio_and_chat(
        vod_input,
        chat_file=chat_input,
        output_dir=cache_dir,
        job_id=job_id,
        progress_callback=on_dl_progress,
    )
    check_cancelled_or_paused(job_id)

    logger.info(f"Audio source ready: {audio_path}")
    logger.info(f"Chat source ready : {resolved_chat_path or 'None (using audio loudness/speech energy)'}")
    log_job_message(job_id, f"Audio ready: {audio_path}")

    # 2. Transcribe Full Stream Audio ONCE
    if progress_callback:
        progress_callback("transcribe", 20, "Transcribing stream audio (word timestamps + VAD)...", {})

    transcript_cache = os.path.join(cache_dir, vod_id, "transcript.json")
    effective_model = model or cfg.get("audio", {}).get("whisper_model", "large-v3")
    full_transcript_data = transcribe_full_audio(
        audio_path=audio_path,
        model_name=effective_model,
        use_cpu=cpu,
        output_json=transcript_cache,
        job_id=job_id,
    )
    check_cancelled_or_paused(job_id)

    if progress_callback:
        progress_callback("detect", 28, "Loading chat data & analyzing hype signals...", {})

    # 3. Load Chat comments if available
    comments = None
    if resolved_chat_path and os.path.isfile(resolved_chat_path):
        try:
            with open(resolved_chat_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                comments = data.get("comments", [])
                logger.info(f"Loaded {len(comments)} chat comments from {resolved_chat_path}")
                log_job_message(job_id, f"Loaded {len(comments)} chat comments.")
        except Exception as e:
            logger.warning(f"Could not load chat JSON ({e}).")
            comments = None

    # 4. Detect Hype Moments on audio / chat
    moments = find_moments(
        comments=comments,
        video_path=audio_path,
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

    # Attach stream metadata and transcript preview to moments
    for m in moments:
        m["vod_input"] = vod_input
        m["vod_id"] = vod_id
        m["audio_path"] = audio_path
        m["quality"] = quality
        if not m.get("preview") and full_transcript_data:
            m["preview"] = get_transcript_text_in_window(full_transcript_data, m["start"], m["end"])[:100]

    log_job_message(job_id, f"Detection complete: found {len(moments)} hype moments.")

    if progress_callback:
        progress_callback(
            "detect",
            35,
            f"Found {len(moments)} hype moments.",
            {"count": len(moments)},
        )

    return audio_path, resolved_chat_path, moments


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
    vod_input: Optional[str] = None,
    full_transcript_data: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Stage 4 & 5: Download needed video sections, slice transcript captions, and render vertical 9:16 shorts."""
    check_cancelled_or_paused(job_id)
    setup_cuda_dlls()
    os.makedirs(output_dir, exist_ok=True)
    cfg = load_config()

    preset = load_preset(preset_name)
    logger.info(f"Rendering {len(moments)} moments with caption preset '{preset.get('name', preset_name)}'...")
    log_job_message(job_id, f"Beginning render loop for {len(moments)} clips...")

    generated_clips: List[Dict[str, Any]] = []
    total = len(moments)
    if total == 0:
        return []

    # If full transcript data wasn't passed directly, attempt to load it from the first moment's vod_id
    if full_transcript_data is None and moments:
        vod_id = moments[0].get("vod_id") or get_vod_id(vod_input or video_path)
        cand_transcript = os.path.join("downloads", vod_id, "transcript.json")
        if os.path.isfile(cand_transcript):
            try:
                with open(cand_transcript, "r", encoding="utf-8") as f:
                    full_transcript_data = json.load(f)
            except Exception:
                pass

    for i, m in enumerate(moments, 1):
        check_cancelled_or_paused(job_id)
        if isinstance(m, dict):
            start_sec = m["start"]
            end_sec = m["end"]
            score = m.get("score", 0.0)
            preview = m.get("preview", "")
            moment_quality = m.get("quality", "1080p")
            source_input = m.get("vod_input") or vod_input or video_path
        else:
            start_sec, end_sec, score = m[:3]
            preview = ""
            moment_quality = "1080p"
            source_input = vod_input or video_path

        clip_base = f"clip_{i:02d}"
        rough_path = os.path.abspath(os.path.join(output_dir, f"rough_{i:02d}.mp4"))
        ass_path = os.path.abspath(os.path.join(output_dir, f"cap_{i:02d}.ass"))
        final_path = os.path.abspath(os.path.join(output_dir, f"{clip_base}.mp4"))

        # Check if already rendered (Resumption)
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

        step_base_pct = 35 + int(((i - 1) / total) * 60)

        # Step A: Download required video section (or cut from local video)
        if progress_callback:
            progress_callback(
                "render",
                step_base_pct,
                f"Fetching video section: clip {i} of {total} ({start_sec}s -> {end_sec}s)",
                {"clip_index": i, "total": total},
            )

        if not (os.path.isfile(rough_path) and os.path.getsize(rough_path) > 1024):
            dur = end_sec - start_sec
            if is_url(source_input):
                # Download ONLY the needed video section with padding
                sec_path, offset = download_video_section(
                    source_input,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    quality=moment_quality,
                    output_dir="downloads",
                    job_id=job_id,
                )
                cut_rough_clip(sec_path, offset, offset + dur, rough_path, use_cpu=cpu, job_id=job_id)
            else:
                # Local video or extracted video
                local_media = os.path.abspath(source_input)
                # If source_input was audio-only from local file, check if original video is available
                if not local_media.endswith((".mp4", ".mkv", ".mov", ".avi", ".webm")):
                    cand_video = local_media.rsplit(".", 1)[0] + ".mp4"
                    if os.path.isfile(cand_video):
                        local_media = cand_video
                cut_rough_clip(local_media, start_sec, end_sec, rough_path, use_cpu=cpu, job_id=job_id)

        check_cancelled_or_paused(job_id)

        # Step B: Subtitles (Slice from full transcript, or fallback to per-clip)
        words: List[Tuple[float, float, str]] = []
        if full_transcript_data:
            words = slice_transcript(full_transcript_data, start_sec, end_sec)

        if not words:
            if progress_callback:
                progress_callback(
                    "transcribe",
                    step_base_pct + int(20 / total),
                    f"Transcribing fallback: clip {i} of {total}",
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

        # Step C: Subtitles ASS
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
            vod_input=vod_input,
        )

    # Detect moments (audio-first + full transcript)
    audio_path, chat_path, detected_moments = detect_pipeline_moments(
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
        model=model,
        cpu=cpu,
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
        video_path=vod_input,
        moments=detected_moments,
        output_dir=output_dir,
        preset_name=preset_name,
        model=model,
        cpu=cpu,
        overlay=overlay,
        job_id=job_id,
        progress_callback=progress_callback,
        vod_input=vod_input,
    )
