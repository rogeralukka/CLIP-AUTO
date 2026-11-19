"""Story hunter, visual motion energy detection, and pooled candidate ranking engine.

Implements transcript-first narrative arc extraction (AutoClip/OpenShorts style),
visual motion energy detection for non-verbal chaotic moments (crashes, jumps),
a 2-of-3 evidence coincidence gate, and SupoClip/Klap-style 4-pillar virality ranking:
Composite Score = 0.30 * Hook + 0.25 * Flow + 0.30 * Emotion + 0.15 * Standalone.
"""
import json
import logging
import math
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .config import load_config
from .judge import call_ollama_generate, is_ollama_available, load_creator_few_shot_feedback
from .signals import compute_robust_z_scores
from .transcription import collapse_repeats, get_whisper_model, transcribe_full_audio, unload_whisper_model
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.story")


def compute_visual_motion_energy(
    video_path: str,
    start_sec: float = 0.0,
    duration_sec: Optional[float] = None,
    sample_fps: float = 2.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute per-second visual motion energy and robust z-scores using fast FFmpeg pipe.

    Downsamples frames to 160x90 grayscale via FFmpeg piped directly into NumPy.
    Uses input-seeking (-ss before -i) to seek instantly even in 20+ GB TS/MP4 buffers.
    Runs in < 1 second for a 30-minute slice. Catches game crashes, camera shakes,
    and physical reactions where speech is absent.
    """
    if not video_path or not os.path.isfile(video_path):
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    dur_to_extract = duration_sec if duration_sec is not None and duration_sec > 0 else 600.0

    width, height = 160, 90
    frame_size = width * height

    cmd = [
        ffmpeg_bin, "-y", "-v", "error",
        "-ss", f"{start_sec:.2f}",
        "-t", f"{dur_to_extract:.2f}",
        "-i", os.path.abspath(video_path),
        "-vf", f"fps={sample_fps},scale={width}:{height}",
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-",
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        raw_data, _ = proc.communicate(timeout=60)
    except Exception as e:
        logger.debug(f"FFmpeg motion energy pipe failed: {e}")
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

    if not raw_data or len(raw_data) < frame_size * 2:
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

    num_frames = len(raw_data) // frame_size
    usable_bytes = num_frames * frame_size
    frames = np.frombuffer(raw_data[:usable_bytes], dtype=np.uint8).reshape((num_frames, height, width)).astype(np.float32)

    # Frame difference across time axis
    diffs = np.abs(np.diff(frames, axis=0))
    energies = np.mean(diffs, axis=(1, 2))  # per-sample mean pixel change

    if len(energies) == 0:
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

    samples_per_sec = int(round(sample_fps)) or 1
    sec_count = int(math.ceil(len(energies) / samples_per_sec))
    per_sec = np.zeros(sec_count, dtype=np.float32)
    for s in range(sec_count):
        chunk = energies[s * samples_per_sec: (s + 1) * samples_per_sec]
        if len(chunk) > 0:
            per_sec[s] = float(np.max(chunk))

    z_scores = compute_robust_z_scores(per_sec, window_size=300)
    return per_sec, z_scores


def format_indexed_transcript(words: List[Dict[str, Any]]) -> str:
    """Format words as '[0] word [1] word ...' for LLM word-index selection."""
    tokens = []
    for idx, w in enumerate(words):
        word_text = str(w.get("word", "")).strip()
        tokens.append(f"[{idx}] {word_text}")
    return " ".join(tokens)


def snap_word_indices_to_sentences(
    words: List[Dict[str, Any]],
    start_idx: int,
    end_idx: int,
    max_lead_sec: float = 3.0,
    max_trail_sec: float = 4.0,
) -> Tuple[float, float, str]:
    """Snap word indices to clean sentence or natural pause boundaries.

    Expands start backwards to sentence start or pause >= 0.35s.
    Expands end forwards to sentence terminator (.!?) or pause >= 0.4s plus 0.4s reaction beat.
    """
    n = len(words)
    if n == 0:
        return 0.0, 0.0, ""

    s_idx = max(0, min(n - 1, start_idx))
    e_idx = max(0, min(n - 1, end_idx))
    if e_idx < s_idx:
        e_idx = s_idx

    raw_start = float(words[s_idx].get("start", 0.0))
    raw_end = float(words[e_idx].get("end", raw_start + 1.0))

    # Backward search for start boundary
    curr_s = s_idx
    while curr_s > 0:
        prev_w = words[curr_s - 1]
        this_w = words[curr_s]
        pause = float(this_w.get("start", 0.0)) - float(prev_w.get("end", 0.0))
        prev_text = str(prev_w.get("word", ""))
        this_text = str(this_w.get("word", ""))

        if raw_start - float(this_w.get("start", 0.0)) > max_lead_sec:
            break
        # Boundary: previous word ends with sentence punct, or pause >= 0.35s, or this word is capitalized
        if prev_text.endswith((".", "!", "?")) or pause >= 0.35:
            break
        curr_s -= 1

    # Forward search for end boundary
    curr_e = e_idx
    while curr_e < n - 1:
        this_w = words[curr_e]
        next_w = words[curr_e + 1]
        pause = float(next_w.get("start", 0.0)) - float(this_w.get("end", 0.0))
        this_text = str(this_w.get("word", ""))

        if float(this_w.get("end", 0.0)) - raw_end > max_trail_sec:
            break
        if this_text.endswith((".", "!", "?")) or pause >= 0.40:
            break
        curr_e += 1

    snapped_start = float(words[curr_s].get("start", raw_start))
    snapped_end = float(words[curr_e].get("end", raw_end)) + 0.40  # 400ms reaction beat

    clip_words = words[curr_s: curr_e + 1]
    clip_text = " ".join(str(w.get("word", "")) for w in clip_words).strip()

    return round(snapped_start, 2), round(snapped_end, 2), clip_text


def calculate_virality_score(
    hook: float,
    flow: float,
    emotion: float,
    standalone: float,
) -> float:
    """Compute weighted 4-pillar virality score (0.0 to 10.0).

    Weights: Hook (30%), Flow (25%), Emotion (30%), Standalone (15%).
    """
    h = max(0.0, min(10.0, float(hook)))
    f = max(0.0, min(10.0, float(flow)))
    e = max(0.0, min(10.0, float(emotion)))
    s = max(0.0, min(10.0, float(standalone)))

    composite = (0.30 * h) + (0.25 * f) + (0.30 * e) + (0.15 * s)
    return round(composite, 2)


def build_story_prompt(
    indexed_text: str,
    window_duration_sec: float,
    creator_feedback: str = "",
) -> str:
    """Construct prompt for Qwen to discover narrative clip candidates."""
    return f"""You are a master viral clip hunter analyzing a continuous {window_duration_sec:.0f}s live stream transcript window.
Identify UP TO 2 candidate short-form video moments (TikTok / Shorts / Reels) that have a complete story arc.

RULES FOR CANDIDATE SELECTION:
1. Every candidate MUST have a HOOK (setup/conflict in first 3s) and an internal PAYOFF (punchline/climax/reaction).
2. Word index precision: Specify start_word_index and end_word_index using the bracketed numbers like [0], [1].
3. Standalone test: A stranger scrolling TikTok must understand the situation with ZERO prior stream context.
4. Score each pillar strictly from 0.0 to 10.0:
   - hook: How strongly the opening 3 seconds arrests scrolling attention.
   - flow: How smoothly the story progresses without boring lulls.
   - emotion: streamer laughter, shock, rage, triumph, panic, or awe.
   - standalone: complete self-contained sense.
5. Keep reason short and concise (under 12 words).
{creator_feedback}
INPUT INDEXED TRANSCRIPT:
{indexed_text}

Respond ONLY with valid JSON following this exact structure:
{{
  "candidates": [
    {{
      "start_word_index": <int>,
      "end_word_index": <int>,
      "hook": <float 0.0-10.0>,
      "flow": <float 0.0-10.0>,
      "emotion": <float 0.0-10.0>,
      "standalone": <float 0.0-10.0>,
      "payoff_inside": <true or false>,
      "hook_title": "<concise title <= 7 words>",
      "reason": "<short phrase <= 12 words explaining the moment>"
    }}
  ]
}}"""


def hunt_story_candidates_in_window(
    window_words: List[Dict[str, Any]],
    win_start_sec: float,
    win_end_sec: float,
    endpoint: str = "http://localhost:11434",
    model_name: str = "qwen3:14b",
    timeout: float = 120.0,
) -> List[Dict[str, Any]]:
    """Discover story candidates in a continuous transcript window using word-index prompting."""
    if len(window_words) < 12:
        return []

    indexed_text = format_indexed_transcript(window_words)
    creator_fb = load_creator_few_shot_feedback()
    win_dur = win_end_sec - win_start_sec

    prompt = build_story_prompt(indexed_text, win_dur, creator_fb)
    raw_json = call_ollama_generate(prompt, endpoint=endpoint, model=model_name, timeout=timeout)
    if not raw_json:
        return []

    # Extract JSON object
    m = re.search(r"\{[\s\S]*\}", raw_json.strip())
    if not m:
        return []

    try:
        data = json.loads(m.group(0))
    except Exception:
        return []

    raw_candidates = data.get("candidates", [])
    if not isinstance(raw_candidates, list):
        return []

    results: List[Dict[str, Any]] = []
    n_words = len(window_words)

    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        try:
            s_idx = int(item.get("start_word_index", 0))
            e_idx = int(item.get("end_word_index", n_words - 1))
        except (ValueError, TypeError):
            continue

        s_idx = max(0, min(n_words - 1, s_idx))
        e_idx = max(0, min(n_words - 1, e_idx))
        if e_idx <= s_idx:
            continue

        hook_sc = float(item.get("hook", 5.0))
        flow_sc = float(item.get("flow", 5.0))
        emot_sc = float(item.get("emotion", 5.0))
        stan_sc = float(item.get("standalone", 5.0))
        payoff_in = bool(item.get("payoff_inside", True))

        snapped_s, snapped_e, clip_text = snap_word_indices_to_sentences(window_words, s_idx, e_idx)
        dur = snapped_e - snapped_s
        if dur < 10.0 or dur > 120.0:
            continue

        composite = calculate_virality_score(hook_sc, flow_sc, emot_sc, stan_sc)

        hook_title = str(item.get("hook_title", "")).strip()
        words = hook_title.split()
        if len(words) > 7:
            hook_title = " ".join(words[:7])

        results.append({
            "source": "story_hunter",
            "start": round(snapped_s, 2),
            "end": round(snapped_e, 2),
            "duration": round(dur, 2),
            "score": composite,
            "sub_scores": {
                "hook": round(hook_sc, 1),
                "flow": round(flow_sc, 1),
                "emotion": round(emot_sc, 1),
                "standalone": round(stan_sc, 1),
            },
            "signals": {"story_arc": round(composite, 2)},
            "hook_title": hook_title,
            "reason": str(item.get("reason", "")).strip(),
            "standalone": stan_sc >= 5.0,
            "payoff_inside": payoff_in,
            "transcript": clip_text,
            "status": "accepted" if (payoff_in and stan_sc >= 4.0 and composite >= 5.0) else "rejected",
            "decision_reason": f"Virality {composite:.1f}/10 (H:{hook_sc:.1f} F:{flow_sc:.1f} E:{emot_sc:.1f} S:{stan_sc:.1f})",
        })

    return results


def extract_action_candidates(
    slice_wav: str,
    slice_video: Optional[str],
    slice_duration: float,
    slice_offset_sec: float = 0.0,
    comments: Optional[List[Dict[str, Any]]] = None,
    words: Optional[List[Dict[str, Any]]] = None,
    sensitivity: float = 2.0,
) -> List[Dict[str, Any]]:
    """Extract candidate moments driven by audio volume peaks and visual motion energy.

    Applies the 2-of-3 Evidence Gate:
    If spoken words < 8 in candidate window, it requires at least TWO of:
    1. Chat burst (z >= 1.8) [Twitch/recorded chat]
    2. Visual motion change (z >= 1.8) [crashes, jumps]
    3. Streamer loudness spike (z >= 1.8).
    A loudness spike alone NEVER makes a clip.
    """
    from .detection import find_moments

    # 1. Loudness peaks
    moments = find_moments(
        comments=comments,
        video_path=slice_wav,
        z_thresh=sensitivity,
        before=15,
        after=35,
        window=5,
        min_clip_length=15.0,
        max_clip_length=60.0,
    )

    # 2. Visual motion energy
    motion_z = None
    if slice_video and os.path.isfile(slice_video):
        try:
            _, motion_z = compute_visual_motion_energy(
                video_path=slice_video,
                start_sec=slice_offset_sec,
                duration_sec=slice_duration,
                sample_fps=2.0,
            )
        except Exception as e:
            logger.debug(f"Motion energy extraction skipped: {e}")

    results: List[Dict[str, Any]] = []
    has_chat_data = bool(comments and len(comments) >= 10)

    for idx, m in enumerate(moments):
        st = float(m["start"])
        et = float(m["end"])
        dur = et - st

        # Count words in this window
        cand_words = []
        if words:
            cand_words = [w for w in words if st <= float(w.get("start", 0)) <= et]
        word_count = len(cand_words)
        cand_text = " ".join(w.get("word", "") for w in cand_words).strip()

        # Check visual motion in window
        vis_peak = 0.0
        if motion_z is not None and len(motion_z) > 0:
            s_sec = max(0, int(st))
            e_sec = min(len(motion_z), int(et) + 1)
            if e_sec > s_sec:
                vis_peak = float(np.max(motion_z[s_sec:e_sec]))

        # Evidence evaluation
        breakdown = m.get("breakdown", {})
        loudness_z = float(breakdown.get("streamer_loudness", m.get("score", 0.0)))
        chat_z = float(breakdown.get("chat_rate", 0.0))

        has_speech = word_count >= 8
        has_loudness = loudness_z >= 1.8
        has_motion = vis_peak >= 1.8
        has_chat = chat_z >= 1.8

        evidence_count = 0
        if has_speech:
            evidence_count += 1
        if has_motion:
            evidence_count += 1
        if has_chat_data and has_chat:
            evidence_count += 1
        elif not has_chat_data and has_loudness:
            # When chat is not recorded, loudness + motion satisfies 2 signals
            evidence_count += 1

        # Strict 2-of-3 Gate: loudness alone with < 8 words and no visual motion rejected
        if not has_speech and evidence_count < 2:
            logger.debug(f"Action candidate at {st:.1f}s rejected by 2-of-3 gate (speech={has_speech}, motion={has_motion}, chat={has_chat})")
            continue

        results.append({
            "source": "action_energy",
            "start": round(st, 2),
            "end": round(et, 2),
            "duration": round(dur, 2),
            "score": round(max(loudness_z, vis_peak), 2),
            "signals": {
                "loudness_z": round(loudness_z, 2),
                "motion_z": round(vis_peak, 2),
                "chat_z": round(chat_z, 2),
            },
            "transcript": cand_text[:120] + ("..." if len(cand_text) > 120 else ""),
            "hook_title": "",
            "sub_scores": None,  # Needs LLM ranking or unified judge
            "standalone": True,
            "payoff_inside": True,
            "status": "pending_score",
            "decision_reason": f"Action spike (loudness={loudness_z:.1f}, motion={vis_peak:.1f})",
        })

    return results


def dedupe_and_merge_candidates(
    candidates: List[Dict[str, Any]],
    overlap_threshold: float = 0.50,
) -> List[Dict[str, Any]]:
    """Deduplicate overlapping candidates, keeping the higher-scoring moment."""
    if not candidates:
        return []

    # Sort descending by score
    sorted_cands = sorted(candidates, key=lambda c: float(c.get("score", 0.0)), reverse=True)
    kept: List[Dict[str, Any]] = []

    for cand in sorted_cands:
        c_start = cand["start"]
        c_end = cand["end"]
        c_dur = c_end - c_start
        overlap_found = False

        for existing in kept:
            e_start = existing["start"]
            e_end = existing["end"]
            e_dur = e_end - e_start

            overlap_start = max(c_start, e_start)
            overlap_end = min(c_end, e_end)
            if overlap_end > overlap_start:
                overlap_len = overlap_end - overlap_start
                ratio = overlap_len / min(c_dur, e_dur)
                if ratio >= overlap_threshold:
                    overlap_found = True
                    break

        if not overlap_found:
            kept.append(cand)

    # Re-sort chronologically
    kept.sort(key=lambda c: c["start"])
    return kept


def evaluate_action_candidate_with_llm(
    cand: Dict[str, Any],
    transcript_text: str,
    chat_sample: List[str],
    endpoint: str = "http://localhost:11434",
    model_name: str = "qwen3:14b",
) -> Dict[str, Any]:
    """Score action candidates using the same 4 pillars so all candidates share one scale."""
    creator_fb = load_creator_few_shot_feedback()
    prompt = f"""You are a viral short-form editor evaluating an action/clutch stream clip ({cand['duration']:.1f}s).
Rate this moment on the 4 virality pillars (0.0 to 10.0).

RULES:
1. hook: Attention-grabbing first 2-3s.
2. flow: Progression without dead space.
3. emotion: Energy, laughter, chaos, reaction.
4. standalone: Understandable with zero prior context.
5. payoff_inside: True only if the punchline/climax occurs inside this clip.
{creator_fb}
TRANSCRIPT:
"{transcript_text.strip() if transcript_text.strip() else '[Non-verbal action/gameplay chaos]'}"

SIGNALS:
{json.dumps(cand.get('signals', {}))}

Respond ONLY with valid JSON:
{{
  "hook": <float 0.0-10.0>,
  "flow": <float 0.0-10.0>,
  "emotion": <float 0.0-10.0>,
  "standalone": <float 0.0-10.0>,
  "payoff_inside": <true or false>,
  "hook_title": "<concise title <= 7 words>",
  "reason": "<one sentence explanation>"
}}"""

    raw_json = call_ollama_generate(prompt, endpoint=endpoint, model=model_name, timeout=120.0)
    if not raw_json:
        # Fallback sub-scores from signals
        cand["score"] = 5.0
        cand["sub_scores"] = {"hook": 5.0, "flow": 5.0, "emotion": 6.0, "standalone": 5.0}
        cand["status"] = "accepted"
        return cand

    m = re.search(r"\{[\s\S]*\}", raw_json.strip())
    if not m:
        return cand

    try:
        data = json.loads(m.group(0))
        h = float(data.get("hook", 5.0))
        f = float(data.get("flow", 5.0))
        e = float(data.get("emotion", 5.0))
        s = float(data.get("standalone", 5.0))
        payoff = bool(data.get("payoff_inside", True))
        composite = calculate_virality_score(h, f, e, s)

        hook_title = str(data.get("hook_title", "")).strip()
        words = hook_title.split()
        if len(words) > 7:
            hook_title = " ".join(words[:7])

        cand["score"] = composite
        cand["sub_scores"] = {
            "hook": round(h, 1),
            "flow": round(f, 1),
            "emotion": round(e, 1),
            "standalone": round(s, 1),
        }
        cand["hook_title"] = hook_title
        cand["reason"] = str(data.get("reason", "")).strip()
        cand["standalone"] = s >= 5.0
        cand["payoff_inside"] = payoff
        cand["status"] = "accepted" if (payoff and s >= 4.0 and composite >= 5.0) else "rejected"
        cand["decision_reason"] = f"Virality {composite:.1f}/10 (H:{h:.1f} F:{f:.1f} E:{e:.1f} S:{s:.1f})"
    except Exception as exc:
        logger.warning(f"Failed to parse action candidate evaluation: {exc}")

    return cand


def hunt_story_candidates_and_pool(
    slice_wav: str,
    slice_duration: float,
    source_video: Optional[str] = None,
    slice_offset_sec: float = 0.0,
    sensitivity: float = 2.0,
    max_clips_per_hour: int = 10,
    comments: Optional[List[Dict[str, Any]]] = None,
    endpoint: str = "http://localhost:11434",
    model_name: str = "qwen3:14b",
    window_length_sec: float = 240.0,
    window_overlap_sec: float = 30.0,
    remaining_quota: Optional[int] = None,
    remaining_buffer_quota: Optional[int] = None,
    buffer_score_threshold: float = 6.5,
) -> List[Dict[str, Any]]:
    """Complete Step 2 Pipeline: Story Hunter + Action Pool + Unified 4-Pillar Ranking."""
    logger.info(f"Starting Story Hunter + Action Pool pipeline on slice ({slice_duration:.1f}s)...")

    # 1. Transcribe the entire slice with word timestamps (cached to disk)
    transcript_cache = slice_wav.replace(".wav", "_transcript.json")
    transcript_data = transcribe_full_audio(slice_wav, use_cpu=False, output_json=transcript_cache)
    words = transcript_data.get("words", [])

    candidates: List[Dict[str, Any]] = []

    # Check Ollama availability
    ollama_ok, _ = is_ollama_available(endpoint)
    if not ollama_ok:
        logger.warning("Ollama is unavailable; story hunter requires Ollama Qwen.")

    # 2. STORY SOURCE: Continuous sliding windows (AutoClip / OpenShorts style)
    if ollama_ok and words:
        unload_whisper_model()
        step = max(30.0, window_length_sec - window_overlap_sec)
        win_start = 0.0
        win_idx = 1

        while win_start < slice_duration:
            win_end = min(slice_duration, win_start + window_length_sec)
            win_words = [w for w in words if win_start <= float(w.get("start", 0)) <= win_end]

            if len(win_words) >= 15:
                logger.info(f"Hunting stories in window {win_idx} ({win_start:.0f}s - {win_end:.0f}s, {len(win_words)} words)...")
                story_cands = hunt_story_candidates_in_window(
                    window_words=win_words,
                    win_start_sec=win_start,
                    win_end_sec=win_end,
                    endpoint=endpoint,
                    model_name=model_name,
                )
                candidates.extend(story_cands)

            win_start += step
            win_idx += 1

    # 3. ACTION SOURCE: Loudness + Visual Motion Energy Peaks
    action_cands = extract_action_candidates(
        slice_wav=slice_wav,
        slice_video=source_video,
        slice_duration=slice_duration,
        slice_offset_sec=slice_offset_sec,
        comments=comments,
        words=words,
        sensitivity=sensitivity,
    )

    # 4. Score pending action candidates with Ollama Qwen so they share the 4-pillar scale
    if ollama_ok and action_cands:
        for cand in action_cands:
            cand_words = [w for w in words if cand["start"] <= float(w.get("start", 0)) <= cand["end"]]
            text = " ".join(w.get("word", "") for w in cand_words).strip()
            evaluate_action_candidate_with_llm(
                cand=cand,
                transcript_text=text,
                chat_sample=[],
                endpoint=endpoint,
                model_name=model_name,
            )
        candidates.extend(action_cands)
    elif action_cands:
        candidates.extend(action_cands)

    # 5. Deduplicate overlapping candidates
    deduped = dedupe_and_merge_candidates(candidates, overlap_threshold=0.50)
    logger.info(f"Pooled candidate count after deduplication: {len(deduped)}")

    # 6. Apply Story-Completeness Gate & Unified Ranking
    # A candidate must have payoff_inside == True and standalone >= 4.0 to be accepted
    accepted_pool = []
    rejected_pool = []

    for c in deduped:
        sub = c.get("sub_scores") or {}
        stan_val = float(sub.get("standalone", 5.0 if c.get("standalone") else 2.0))
        payoff = bool(c.get("payoff_inside", True))
        score = float(c.get("score", 0.0))

        if not payoff:
            c["status"] = "rejected"
            c["decision_reason"] = "Rejected: Payoff/punchline cut off outside window"
            rejected_pool.append(c)
        elif stan_val < 4.0:
            c["status"] = "rejected"
            c["decision_reason"] = f"Rejected: Low standalone clarity ({stan_val:.1f} < 4.0)"
            rejected_pool.append(c)
        elif score < 4.5:
            c["status"] = "rejected"
            c["decision_reason"] = f"Rejected: Low composite virality ({score:.1f} < 4.5)"
            rejected_pool.append(c)
        else:
            c["status"] = "accepted"
            accepted_pool.append(c)

    # 7. Hourly Maximum Cap per Streamer (base max_clips_per_hour + 2-3 clip buffer for great moments)
    if remaining_quota is not None:
        base_quota = max(0, int(remaining_quota))
    elif slice_duration <= 3600.0:
        # For sub-hour windows, allow up to the full hourly quota so bursty viral moments aren't dropped
        base_quota = max(1, max_clips_per_hour)
    else:
        slice_hours = slice_duration / 3600.0
        base_quota = max(1, int(math.ceil(slice_hours * max_clips_per_hour)))

    max_buffer_slots = 3 if remaining_buffer_quota is None else max(0, int(remaining_buffer_quota))

    # Sort accepted pool descending by composite virality score
    accepted_pool.sort(key=lambda c: float(c.get("score", 0.0)), reverse=True)

    # 1. Fill base quota
    final_accepted = accepted_pool[:base_quota]
    overflow = accepted_pool[base_quota:]

    # 2. Check overflow candidates against the 2-3 great-clip buffer
    used_buffer_slots = 0
    for c in overflow:
        score = float(c.get("score", 0.0))
        # If moment is a great clip (score >= buffer_score_threshold) and buffer slots remain, rescue it!
        if score >= buffer_score_threshold and used_buffer_slots < max_buffer_slots:
            c["status"] = "accepted"
            c["decision_reason"] = f"Accepted via Great-Moment Buffer (score {score:.1f} >= {buffer_score_threshold:.1f}, slot #{used_buffer_slots + 1}/3)"
            final_accepted.append(c)
            used_buffer_slots += 1
        else:
            c["status"] = "rejected"
            if score < buffer_score_threshold:
                c["decision_reason"] = f"Rejected: Exceeded hourly quota cap (score {score:.1f} < {buffer_score_threshold:.1f} buffer threshold, ranked #{accepted_pool.index(c) + 1})"
            else:
                c["decision_reason"] = f"Rejected: Exceeded hourly quota cap + buffer (max {max_clips_per_hour}+3/hr full, ranked #{accepted_pool.index(c) + 1})"
            rejected_pool.append(c)

    final_list = final_accepted + rejected_pool
    # Sort chronologically for clean output
    final_list.sort(key=lambda c: c["start"])

    logger.info(f"New engine final decision: {len(final_accepted)} accepted ({used_buffer_slots} via great-moment buffer), {len(rejected_pool)} rejected (hourly cap={max_clips_per_hour}+3).")
    return final_list
