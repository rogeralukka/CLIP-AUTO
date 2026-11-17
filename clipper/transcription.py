"""Transcription module with faster-whisper, full stream caching, and word timestamp slicing."""
import gc
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .config import load_config
from .process_manager import check_cancelled_or_paused
from .utils import setup_cuda_dlls

logger = logging.getLogger("clipper.transcription")

_LOADED_MODEL = None
_LOADED_MODEL_PARAMS: Tuple[str, str, str] = ("", "", "")


def unload_whisper_model():
    """Unload Whisper model from memory and release GPU VRAM (12GB VRAM constraint)."""
    global _LOADED_MODEL, _LOADED_MODEL_PARAMS
    if _LOADED_MODEL is not None:
        logger.info("Unloading Whisper model from GPU memory...")
        del _LOADED_MODEL
        _LOADED_MODEL = None
        _LOADED_MODEL_PARAMS = ("", "", "")
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        logger.info("Whisper model unloaded and VRAM cleared.")


def get_whisper_model(
    model_name: Optional[str] = None,
    use_cpu: bool = False
):
    """Retrieve or initialize the cached WhisperModel instance."""
    global _LOADED_MODEL, _LOADED_MODEL_PARAMS

    setup_cuda_dlls()
    cfg = load_config()
    target_model = model_name or cfg.get("audio", {}).get("whisper_model", "large-v3")
    target_device = "cpu" if use_cpu else "cuda"
    target_compute = "int8" if use_cpu else "float16"

    current_params = (target_model, target_device, target_compute)
    if _LOADED_MODEL is not None and _LOADED_MODEL_PARAMS == current_params:
        return _LOADED_MODEL

    from faster_whisper import WhisperModel

    logger.info(
        f"Loading Whisper model '{target_model}' on {target_device.upper()} ({target_compute})..."
    )
    try:
        model = WhisperModel(target_model, device=target_device, compute_type=target_compute)
    except Exception as e:
        if not use_cpu:
            logger.warning(
                f"Failed to load Whisper on CUDA ({e}). Falling back to CPU with int8..."
            )
            target_device = "cpu"
            target_compute = "int8"
            current_params = (target_model, target_device, target_compute)
            model = WhisperModel(target_model, device=target_device, compute_type=target_compute)
        else:
            raise e

    _LOADED_MODEL = model
    _LOADED_MODEL_PARAMS = current_params
    return _LOADED_MODEL


def transcribe_full_audio(
    audio_path: str,
    model_name: Optional[str] = None,
    use_cpu: bool = False,
    output_json: Optional[str] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe full stream audio ONCE with faster-whisper.

    Caches results to output_json (e.g. downloads/<vod_id>/transcript.json).
    Returns dict with 'words', 'segments', and metadata.
    """
    check_cancelled_or_paused(job_id)
    abs_audio = os.path.abspath(audio_path)

    if output_json:
        abs_json = os.path.abspath(output_json)
        os.makedirs(os.path.dirname(abs_json), exist_ok=True)
        if os.path.isfile(abs_json) and os.path.getsize(abs_json) > 10:
            try:
                with open(abs_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("words") is not None:
                        logger.info(f"Loaded {len(data['words'])} words from full transcript cache: {abs_json}")
                        return data
            except Exception as e:
                logger.warning(f"Failed to read full transcript cache {abs_json}: {e}. Re-transcribing.")

    cfg = load_config()
    vad_on = cfg.get("audio", {}).get("vad_filter", True)

    model = get_whisper_model(model_name=model_name, use_cpu=use_cpu)
    logger.info(f"Transcribing full audio '{os.path.basename(abs_audio)}' with word timestamps (VAD={vad_on})...")

    check_cancelled_or_paused(job_id)
    segments_gen, info = model.transcribe(
        abs_audio,
        word_timestamps=True,
        vad_filter=vad_on,
        vad_parameters=dict(min_silence_duration_ms=500) if vad_on else None,
    )

    words_list: List[Dict[str, Any]] = []
    segments_list: List[Dict[str, Any]] = []

    for sg in segments_gen:
        check_cancelled_or_paused(job_id)
        seg_record = {
            "start": round(sg.start, 3),
            "end": round(sg.end, 3),
            "text": sg.text.strip(),
        }
        segments_list.append(seg_record)

        for w in sg.words or []:
            word_str = w.word.strip()
            if word_str:
                words_list.append({
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "word": word_str,
                    "probability": round(getattr(w, "probability", 1.0), 3),
                })

    result_data = {
        "source": abs_audio,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "duration": round(info.duration, 2),
        "segments": segments_list,
        "words": words_list,
    }

    if output_json:
        try:
            with open(abs_json, "w", encoding="utf-8") as f:
                json.dump(result_data, f, indent=2)
            logger.info(f"Saved full transcript to: {abs_json}")
        except Exception as e:
            logger.warning(f"Could not save full transcript JSON: {e}")

    return result_data


def slice_transcript(
    full_transcript_data: Dict[str, Any],
    start_sec: float,
    end_sec: float,
) -> List[Tuple[float, float, str]]:
    """Slice words from full transcript for a specific moment and offset to clip time (0.0 - duration).

    Returns:
        List of (clip_start_sec, clip_end_sec, word_text)
    """
    words = full_transcript_data.get("words", [])
    if not words:
        return []

    sliced: List[Tuple[float, float, str]] = []
    clip_dur = max(0.1, end_sec - start_sec)

    for w in words:
        w_start = w["start"]
        w_end = w["end"]
        # Include words overlapping with the clip window
        if w_end >= start_sec and w_start <= end_sec:
            rel_start = max(0.0, round(w_start - start_sec, 3))
            rel_end = min(clip_dur, round(w_end - start_sec, 3))
            if rel_end > rel_start:
                sliced.append((rel_start, rel_end, w["word"]))

    return sliced


def get_transcript_text_in_window(
    full_transcript_data: Dict[str, Any],
    start_sec: float,
    end_sec: float,
) -> str:
    """Extract plain text transcript within a time window (for LLM context and previews)."""
    words = full_transcript_data.get("words", [])
    window_words = [
        w["word"] for w in words
        if w["end"] >= start_sec and w["start"] <= end_sec
    ]
    if window_words:
        return " ".join(window_words)

    # Fallback to segments
    segments = full_transcript_data.get("segments", [])
    window_segs = [
        s["text"] for s in segments
        if s["end"] >= start_sec and s["start"] <= end_sec
    ]
    return " ".join(window_segs).strip()


def transcribe_clip(
    media_path: str,
    model_name: str = "large-v3",
    use_cpu: bool = False,
    cache_dir: Optional[str] = None,
    job_id: Optional[str] = None,
) -> List[Tuple[float, float, str]]:
    """Per-clip transcription fallback when full stream transcript is unavailable.

    Returns:
        List of (start_sec, end_sec, word_text)
    """
    check_cancelled_or_paused(job_id)
    abs_media = os.path.abspath(media_path)
    base_no_ext, _ = os.path.splitext(abs_media)

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        clip_name = os.path.basename(base_no_ext)
        cache_file = os.path.join(cache_dir, f"{clip_name}.transcript.json")
    else:
        cache_file = f"{base_no_ext}.transcript.json"

    # Check cache
    if os.path.isfile(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                words = [(w["start"], w["end"], w["word"]) for w in data.get("words", [])]
                logger.info(f"Loaded {len(words)} words from transcript cache: {cache_file}")
                return words
        except Exception as e:
            logger.warning(f"Failed to read transcript cache {cache_file}: {e}. Re-transcribing.")

    model = get_whisper_model(model_name=model_name, use_cpu=use_cpu)
    logger.info(f"Transcribing {os.path.basename(media_path)} with word timestamps...")

    check_cancelled_or_paused(job_id)
    cfg = load_config()
    vad_on = cfg.get("audio", {}).get("vad_filter", True)
    segments, info = model.transcribe(
        abs_media,
        word_timestamps=True,
        vad_filter=vad_on,
        vad_parameters=dict(min_silence_duration_ms=500) if vad_on else None,
    )

    words: List[Tuple[float, float, str]] = []
    saved_records: List[Dict[str, Any]] = []

    for sg in segments:
        check_cancelled_or_paused(job_id)
        for w in sg.words or []:
            word_str = w.word.strip()
            if word_str:
                words.append((w.start, w.end, word_str))
                saved_records.append({
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "word": word_str,
                    "probability": round(getattr(w, "probability", 1.0), 3),
                })

    # Save to cache
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({
                "source": abs_media,
                "language": info.language,
                "language_probability": round(info.language_probability, 3),
                "words": saved_records
            }, f, indent=2)
        logger.info(f"Saved transcript cache: {cache_file}")
    except Exception as e:
        logger.warning(f"Could not write transcript cache: {e}")

    return words
