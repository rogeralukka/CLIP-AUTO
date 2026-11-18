"""Transcription module with faster-whisper, full stream caching, and word timestamp slicing."""
import gc
import json
import re
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    progress_callback: Optional[Callable[[float, float, float], None]] = None,
) -> Dict[str, Any]:
    """Transcribe full stream audio ONCE with faster-whisper.

    progress_callback(done_seconds, total_seconds, elapsed_wall_seconds) is called as each
    piece of the transcript comes out, so the UI can show real progress and a real ETA.

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
                        data["words"] = collapse_repeats(data["words"])
                        return data
            except Exception as e:
                logger.warning(f"Failed to read full transcript cache {abs_json}: {e}. Re-transcribing.")

    cfg = load_config()
    vad_on = cfg.get("audio", {}).get("vad_filter", True)

    model = get_whisper_model(model_name=model_name, use_cpu=use_cpu)
    logger.info(f"Transcribing full audio '{os.path.basename(abs_audio)}' with word timestamps (VAD={vad_on})...")

    check_cancelled_or_paused(job_id)
    t0 = time.time()
    segments_gen, info = None, None
    on_gpu = _LOADED_MODEL_PARAMS[1] == "cuda"
    batch_size = int(cfg.get("audio", {}).get("whisper_batch_size", 16))
    if on_gpu and batch_size > 1:
        # Batched inference is several times faster on a GPU for long streams
        try:
            from faster_whisper import BatchedInferencePipeline
            batched = BatchedInferencePipeline(model=model)
            segments_gen, info = batched.transcribe(
                abs_audio, word_timestamps=True, batch_size=batch_size, vad_filter=True,
                **_ANTI_LOOP,
            )
            logger.info(f"Using batched Whisper (batch_size={batch_size}).")
        except Exception as e:
            logger.warning(f"Batched Whisper unavailable ({e}); using standard mode.")
            segments_gen = None
    if segments_gen is None:
        segments_gen, info = model.transcribe(
            abs_audio,
            word_timestamps=True,
            vad_filter=vad_on,
            vad_parameters=dict(min_silence_duration_ms=500) if vad_on else None,
            condition_on_previous_text=False,
            **_ANTI_LOOP,
        )

    total_sec = float(getattr(info, "duration", 0.0) or 0.0)
    if progress_callback:
        progress_callback(0.0, total_sec, time.time() - t0)

    words_list: List[Dict[str, Any]] = []
    segments_list: List[Dict[str, Any]] = []
    last_report = 0.0

    for sg in segments_gen:
        check_cancelled_or_paused(job_id)
        if progress_callback and time.time() - last_report >= 1.0:
            last_report = time.time()
            try:
                progress_callback(float(sg.end), total_sec, last_report - t0)
            except Exception:
                pass
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

    words_list = collapse_repeats(words_list)
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


# Whisper sometimes gets stuck repeating a phrase ("I know what it is" x20, "oh oh oh ...")
# over music, crowd noise or chanting. These settings make that rarer...
_ANTI_LOOP = dict(
    repetition_penalty=1.1,
    compression_ratio_threshold=2.2,
)


def collapse_repeats(words: List[Dict[str, Any]], max_repeats: int = 3, max_n: int = 6) -> List[Dict[str, Any]]:
    """...and this cleans up what still gets through: a word or phrase (up to 6 words)
    repeated back to back more than `max_repeats` times is kept only `max_repeats` times.
    Captions stay readable and the hook can't become "What What What What"."""
    if not words:
        return words

    def norm(w):
        return re.sub(r"[^\w']", "", str(w.get("word", ""))).lower()

    toks = [norm(w) for w in words]
    keep = [True] * len(words)
    i = 0
    while i < len(words):
        done = False
        for n in range(1, max_n + 1):
            if i + 2 * n > len(words) or not all(toks[i:i + n]):
                continue
            unit = toks[i:i + n]
            reps = 1
            j = i + n
            while j + n <= len(words) and toks[j:j + n] == unit:
                reps += 1
                j += n
            if reps > max_repeats:
                for k in range(i + n * max_repeats, j):
                    keep[k] = False
                i = j
                done = True
                break
        if not done:
            i += 1
    out = [w for w, k in zip(words, keep) if k]
    if len(out) < len(words):
        logger.info(f"Removed {len(words) - len(out)} looped/repeated words from the transcript")
    return out


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
        condition_on_previous_text=False,
        **_ANTI_LOOP,
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
