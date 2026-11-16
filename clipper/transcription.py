"""Transcription module with faster-whisper, CUDA support, and JSON result caching."""
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .process_manager import check_cancelled_or_paused
from .utils import setup_cuda_dlls

logger = logging.getLogger("clipper.transcription")

_LOADED_MODEL = None
_LOADED_MODEL_PARAMS: Tuple[str, str, str] = ("", "", "")


def get_whisper_model(
    model_name: str = "large-v3",
    use_cpu: bool = False
):
    """Retrieve or initialize the cached WhisperModel instance."""
    global _LOADED_MODEL, _LOADED_MODEL_PARAMS

    setup_cuda_dlls()
    target_device = "cpu" if use_cpu else "cuda"
    target_compute = "int8" if use_cpu else "float16"

    current_params = (model_name, target_device, target_compute)
    if _LOADED_MODEL is not None and _LOADED_MODEL_PARAMS == current_params:
        return _LOADED_MODEL

    from faster_whisper import WhisperModel

    logger.info(
        f"Loading Whisper model '{model_name}' on {target_device.upper()} ({target_compute})..."
    )
    try:
        model = WhisperModel(model_name, device=target_device, compute_type=target_compute)
    except Exception as e:
        if not use_cpu:
            logger.warning(
                f"Failed to load Whisper on CUDA ({e}). Falling back to CPU with int8..."
            )
            target_device = "cpu"
            target_compute = "int8"
            current_params = (model_name, target_device, target_compute)
            model = WhisperModel(model_name, device=target_device, compute_type=target_compute)
        else:
            raise e

    _LOADED_MODEL = model
    _LOADED_MODEL_PARAMS = current_params
    return _LOADED_MODEL


def transcribe_clip(
    media_path: str,
    model_name: str = "large-v3",
    use_cpu: bool = False,
    cache_dir: Optional[str] = None,
    job_id: Optional[str] = None,
) -> List[Tuple[float, float, str]]:
    """Transcribe a cut video or audio file with word-level timestamps.

    Results are cached to a JSON file alongside the media file (or in cache_dir)
    so re-running the pipeline on existing clips takes 0.0 seconds.

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
    segments, info = model.transcribe(
        abs_media,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
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
