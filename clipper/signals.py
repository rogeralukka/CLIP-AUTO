"""Hype curve multi-signal engine, robust z-scores, keyword scoring, smart boundary snapping, and deduplication."""
import json
import logging
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import yaml

from .config import load_config
from .process_manager import check_cancelled, run_tracked_command
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.signals")

# Default keyword weights fallback if hype_keywords.yaml cannot be read
DEFAULT_KEYWORDS = {
    "clip it": 3.0, "clip that": 3.0, "clip": 2.0, "lmao": 2.0, "lmfao": 2.5,
    "kekw": 2.5, "omegalul": 2.5, "lul": 1.5, "lol": 1.0, "w": 1.5, "l": 1.2,
    "no way": 2.5, "bro": 1.2, "omg": 2.0, "wtf": 2.0, "clutch": 2.5, "ace": 2.5,
    "insane": 2.0, "gg": 1.5, "pog": 2.5, "poggers": 2.5, "sheesh": 2.0, "bruh": 1.5
}


def load_hype_keywords(yaml_path: Optional[str] = None) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Load weighted chat keywords and patterns from presets/hype_keywords.yaml."""
    target_path = yaml_path
    if not target_path or not os.path.isfile(target_path):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target_path = os.path.join(base_dir, "presets", "hype_keywords.yaml")

    if os.path.isfile(target_path):
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                keywords = data.get("keywords", DEFAULT_KEYWORDS)
                patterns = data.get("patterns", {})
                return keywords, patterns
        except Exception as e:
            logger.warning(f"Could not parse hype keywords YAML ({e}); using defaults.")

    return DEFAULT_KEYWORDS, {}


def compute_robust_z_scores(values: np.ndarray, window_size: int = 600) -> np.ndarray:
    """Compute per-second robust z-score: (value - median) / (1.4826 * MAD) over a rolling window.

    Window defaults to 600s (10 minutes) for localized baseline estimation.
    """
    n = len(values)
    if n == 0:
        return np.array([], dtype=np.float32)

    half_win = max(1, window_size // 2)
    z_scores = np.zeros(n, dtype=np.float32)

    # Use sliding window over stream timeline
    for i in range(n):
        left = max(0, i - half_win)
        right = min(n, i + half_win + 1)
        win = values[left:right]

        med = np.median(win)
        mad = np.median(np.abs(win - med))
        scale = 1.4826 * mad

        if scale < 1e-4:
            # Fall back to standard deviation if MAD is zero (e.g. mostly zeros in window)
            std = np.std(win)
            scale = std if std > 1e-4 else 1.0

        val = values[i]
        # Only consider positive spikes above median as hype
        z = (val - med) / scale
        z_scores[i] = max(0.0, float(z))

    return z_scores


def calculate_chat_signals(
    comments: List[Dict[str, Any]],
    duration: int,
    keywords_dict: Optional[Dict[str, float]] = None,
    patterns_dict: Optional[Dict[str, Any]] = None,
) -> Tuple[np.ndarray, np.ndarray, Dict[int, List[str]]]:
    """Calculate raw per-second chat rate and keyword match score arrays.

    Returns:
        (chat_rate_array, chat_keywords_array, msgs_by_sec)
    """
    if keywords_dict is None or patterns_dict is None:
        kw, pat = load_hype_keywords()
        keywords_dict = keywords_dict or kw
        patterns_dict = patterns_dict or pat

    compiled_kw = [(re.compile(r"(?<!\w)" + re.escape(str(kw).lower()) + r"(?!\w)"), float(w))
                   for kw, w in keywords_dict.items()]
    chat_rate = np.zeros(duration, dtype=np.float32)
    chat_keywords = np.zeros(duration, dtype=np.float32)
    msgs_by_sec: Dict[int, List[str]] = {s: [] for s in range(duration)}

    for c in comments:
        offset = c.get("content_offset_seconds")
        if offset is None:
            continue
        sec = int(offset)
        if 0 <= sec < duration:
            chat_rate[sec] += 1.0
            body = c.get("message", {}).get("body", "")
            if body:
                msgs_by_sec[sec].append(body)

                body_lower = body.lower()
                kw_score = 0.0

                # 1. Match keywords
                for kw_re, weight in compiled_kw:
                    if kw_re.search(body_lower):
                        kw_score += weight

                # 2. Match question wall ("???")
                q_pat = patterns_dict.get("question_wall", {}).get("regex", r"\?{3,}")
                if re.search(q_pat, body):
                    kw_score += patterns_dict.get("question_wall", {}).get("weight", 2.0)

                # 3. Match exclamation wall ("!!!")
                ex_pat = patterns_dict.get("exclamation_wall", {}).get("regex", r"!{3,}")
                if re.search(ex_pat, body):
                    kw_score += patterns_dict.get("exclamation_wall", {}).get("weight", 2.0)

                # 4. ALL CAPS message
                letters = [ch for ch in body if ch.isalpha()]
                if len(letters) >= patterns_dict.get("all_caps", {}).get("min_length", 5):
                    if all(ch.isupper() for ch in letters):
                        kw_score += patterns_dict.get("all_caps", {}).get("weight", 1.5)

                # 5. Repeated words / emote spam (same token repeated 3+ times)
                tokens = body_lower.split()
                if tokens:
                    counts = {}
                    for t in tokens:
                        counts[t] = counts.get(t, 0) + 1
                    if any(cnt >= patterns_dict.get("repeated_words", {}).get("min_repetitions", 3) for cnt in counts.values()):
                        kw_score += patterns_dict.get("repeated_words", {}).get("weight", 2.0)

                chat_keywords[sec] += kw_score

    return chat_rate, chat_keywords, msgs_by_sec


def extract_audio_loudness_curve(
    media_path: str,
    duration: int,
    job_id: Optional[str] = None,
    progress_cb: Optional[Any] = None,
) -> np.ndarray:
    """Extract momentary loudness (LUFS) per second using FFmpeg ebur128."""
    check_cancelled(job_id)
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    cmd = [
        ffmpeg_bin, "-y",
        "-i", media_path,
        "-vn",
        "-af", "ebur128=peak=none",
        "-f", "null",
        "-"
    ]

    logger.info(f"Extracting momentary loudness with FFmpeg ebur128 on {os.path.basename(media_path)}...")
    lines: List[str] = []

    last = [0.0]
    t_re = re.compile(r"t:\s*([\d\.]+)")

    def on_line(l: str):
        lines.append(l)
        if progress_cb and duration > 0:
            m = t_re.search(l)
            if m:
                t = float(m.group(1))
                if t - last[0] >= 30.0:
                    last[0] = t
                    try:
                        progress_cb(min(1.0, t / duration))
                    except Exception:
                        pass

    try:
        run_tracked_command(cmd, job_id=job_id, line_callback=on_line)
    except Exception as e:
        logger.warning(f"Audio loudness extraction failed ({e}); using zero curve.")
        return np.zeros(duration, dtype=np.float32)

    pattern = re.compile(r"t:\s*([\d\.]+)\s+TARGET:[^M]+M:\s*([-\d\.]+)")
    loudness = np.full(duration, -70.0, dtype=np.float32)

    for line in lines:
        m = pattern.search(line)
        if m:
            try:
                t = float(m.group(1))
                m_val = float(m.group(2))
                sec = int(t)
                if 0 <= sec < duration:
                    if m_val > loudness[sec]:
                        loudness[sec] = m_val
            except ValueError:
                continue

    # Convert negative LUFS (-70 to 0) to positive intensity: (M + 70)
    pos_loudness = np.maximum(0.0, loudness + 70.0)
    return pos_loudness


def extract_speech_energy_curve(
    full_transcript_data: Optional[Dict[str, Any]],
    duration: int,
) -> np.ndarray:
    """Extract speech energy curve from transcript: word rate, laughter, and exclamations."""
    if not full_transcript_data:
        return np.zeros(duration, dtype=np.float32)

    words = full_transcript_data.get("words", [])
    if not words:
        return np.zeros(duration, dtype=np.float32)

    energy = np.zeros(duration, dtype=np.float32)
    laughter_tokens = {"haha", "hahaha", "hahahaha", "lol", "lmao", "lmfao", "kekw", "giggle"}

    for w in words:
        start_sec = w.get("start", 0.0)
        end_sec = w.get("end", 0.0)
        word_str = w.get("word", "").strip().lower()

        sec = int(start_sec)
        if 0 <= sec < duration:
            # Baseline word presence
            energy[sec] += 1.0

            # Laughter bonus
            clean_word = re.sub(r"[^\w]", "", word_str)
            if clean_word in laughter_tokens or "haha" in clean_word:
                energy[sec] += 3.0

            # Exclamation bonus
            if "!" in word_str or "?" in word_str:
                energy[sec] += 1.5

    # Smooth speech energy with rolling 3-second window
    kernel = np.ones(3, dtype=np.float32) / 3.0
    smoothed = np.convolve(energy, kernel, mode="same")
    return smoothed


def extract_youtube_heatmap_curve(
    heatmap_path_or_data: Any,
    duration: int,
) -> np.ndarray:
    """Map YouTube heatmap data (most replayed) onto per-second curve."""
    curve = np.zeros(duration, dtype=np.float32)
    data = heatmap_path_or_data

    if isinstance(data, str) and os.path.isfile(data):
        try:
            with open(data, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None

    if not isinstance(data, list):
        return curve

    for item in data:
        st = item.get("start_time", 0.0)
        et = item.get("end_time", 0.0)
        val = item.get("value", 0.0)

        s_idx = max(0, int(st))
        e_idx = min(duration, int(math.ceil(et)))
        if s_idx < e_idx:
            curve[s_idx:e_idx] = np.maximum(curve[s_idx:e_idx], float(val) * 100.0)

    return curve


def build_hype_curve(
    duration: int,
    comments: Optional[List[Dict[str, Any]]] = None,
    media_path: Optional[str] = None,
    full_transcript_data: Optional[Dict[str, Any]] = None,
    heatmap_data: Optional[Any] = None,
    window_size: int = 600,
    job_id: Optional[str] = None,
    progress_cb: Optional[Any] = None,
) -> Tuple[np.ndarray, Dict[str, np.ndarray], Dict[int, List[str]]]:
    """Build multi-signal composite hype curve using robust z-scores and re-normalized weights.

    Returns:
        (composite_hype_curve, signal_z_breakdowns, msgs_by_sec)
    """
    check_cancelled(job_id)
    cfg = load_config()
    weights_cfg = cfg.get("signals", {}).get("weights", {
        "chat_rate": 0.25,
        "chat_keywords": 0.30,
        "streamer_loudness": 0.20,
        "speech_energy": 0.15,
        "youtube_heatmap": 0.10,
    })

    dur = max(10, duration)
    breakdown_z: Dict[str, np.ndarray] = {}
    msgs_by_sec: Dict[int, List[str]] = {}

    # Signal 1 & 2: Chat Rate & Keywords
    if comments and len(comments) > 0:
        c_rate, c_kw, msgs_by_sec = calculate_chat_signals(comments, dur)
        breakdown_z["chat_rate"] = compute_robust_z_scores(c_rate, window_size=window_size)
        breakdown_z["chat_keywords"] = compute_robust_z_scores(c_kw, window_size=window_size)
    else:
        breakdown_z["chat_rate"] = np.zeros(dur, dtype=np.float32)
        breakdown_z["chat_keywords"] = np.zeros(dur, dtype=np.float32)

    # Signal 3: Streamer Loudness
    if media_path and os.path.isfile(media_path):
        loudness_raw = extract_audio_loudness_curve(media_path, dur, job_id=job_id, progress_cb=progress_cb)
        breakdown_z["streamer_loudness"] = compute_robust_z_scores(loudness_raw, window_size=window_size)
    else:
        breakdown_z["streamer_loudness"] = np.zeros(dur, dtype=np.float32)

    # Signal 4: Speech Energy
    if full_transcript_data and full_transcript_data.get("words"):
        speech_raw = extract_speech_energy_curve(full_transcript_data, dur)
        breakdown_z["speech_energy"] = compute_robust_z_scores(speech_raw, window_size=window_size)
    else:
        breakdown_z["speech_energy"] = np.zeros(dur, dtype=np.float32)

    # Signal 5: YouTube Heatmap
    if heatmap_data:
        heatmap_raw = extract_youtube_heatmap_curve(heatmap_data, dur)
        breakdown_z["youtube_heatmap"] = compute_robust_z_scores(heatmap_raw, window_size=window_size)
    else:
        breakdown_z["youtube_heatmap"] = np.zeros(dur, dtype=np.float32)

    # Calculate active weights and re-normalize
    active_weights: Dict[str, float] = {}
    for sig_name, z_arr in breakdown_z.items():
        if np.any(z_arr > 0.01):
            active_weights[sig_name] = float(weights_cfg.get(sig_name, 0.2))

    total_weight = sum(active_weights.values())
    composite_curve = np.zeros(dur, dtype=np.float32)

    # Cap each signal so one of them can't drown out the rest (the YouTube replay graph is
    # blocky and used to decide everything on its own).
    caps = {"youtube_heatmap": 4.0}
    if total_weight > 0:
        agree = np.zeros(dur, dtype=np.float32)
        for sig_name, w in active_weights.items():
            norm_w = w / total_weight
            z = np.minimum(breakdown_z[sig_name], caps.get(sig_name, 6.0))
            composite_curve += norm_w * z
            agree += (z >= 1.5).astype(np.float32)
        # Bonus when several signals spike together (chat + loud + fast talk = real moment)
        composite_curve += 0.35 * np.maximum(0.0, agree - 1.0)
    else:
        # Fallback if all signals are completely flat
        composite_curve = breakdown_z.get("chat_rate", np.zeros(dur, dtype=np.float32))

    return composite_curve, breakdown_z, msgs_by_sec


def _sentence_marks(words: List[Dict[str, Any]], lo: float, hi: float, pause: float = 0.6):
    """Natural cut points between lo and hi.

    starts: times where a sentence/thought begins (after . ! ? or after a pause)
    ends:   times where one finishes (word ending in . ! ? or followed by a pause)
    Each item is (time, strength) where strength grows with the pause length.
    """
    import bisect
    if not words:
        return [], []
    keys = [float(w.get("start", 0.0)) for w in words]
    i0 = max(0, bisect.bisect_left(keys, lo - 2.0) - 1)
    i1 = min(len(words), bisect.bisect_right(keys, hi + 2.0) + 1)
    starts, ends = [], []
    for i in range(i0, i1):
        w = words[i]
        ws, we = float(w.get("start", 0.0)), float(w.get("end", 0.0))
        txt = str(w.get("word", "")).strip()
        prev = words[i - 1] if i > 0 else None
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap_before = ws - float(prev["end"]) if prev else 9.0
        gap_after = float(nxt["start"]) - we if nxt else 9.0
        prev_txt = str(prev.get("word", "")).strip() if prev else ""
        if lo <= ws <= hi and (gap_before >= pause or prev_txt.endswith((".", "!", "?"))):
            # cut a little before the first word, but never into the previous word
            t = max(ws - 0.25, (float(prev["end"]) + 0.05) if prev else 0.0)
            starts.append((t, min(3.0, gap_before) + (0.5 if prev_txt.endswith((".", "!", "?")) else 0.0)))
        if lo <= we <= hi and (gap_after >= pause or txt.endswith((".", "!", "?"))):
            # leave room for the laugh / breath after the last word, stop before the next one
            t = min(we + 0.6, (float(nxt["start"]) - 0.05) if nxt else we + 0.6)
            ends.append((t, min(3.0, gap_after) + (0.5 if txt.endswith((".", "!", "?")) else 0.0)))
    return starts, ends


_STOP = set("""a an the and or but so if then than that this these those it its it's i i'm im me my we
our you your he him his she her they them their is are was were be been being am do does did done
have has had of to in on at for with from by as up out about into over just like yeah yes no okay ok
oh uh um hmm right well really very can could would should will gonna wanna got get go going know
what when where who how why not dont don't there here all some any one two bro dude man""".split())


def build_sentences(words: List[Dict[str, Any]], pause: float = 0.6) -> List[Dict[str, Any]]:
    """Group transcript words into sentences / thoughts (split on . ! ? and on pauses)."""
    sents: List[Dict[str, Any]] = []
    cur: List[Dict[str, Any]] = []
    for w in words:
        if cur and float(w.get("start", 0.0)) - float(cur[-1].get("end", 0.0)) >= pause:
            sents.append(cur)
            cur = []
        cur.append(w)
        if str(w.get("word", "")).strip().endswith((".", "!", "?")):
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    out = []
    prev_end = None
    for sw in sents:
        st, en = float(sw[0]["start"]), float(sw[-1]["end"])
        text = " ".join(str(x.get("word", "")).strip() for x in sw)
        toks = [t for t in re.findall(r"\w+", text.lower()) if len(t) > 2 and t not in _STOP]
        out.append({"start": st, "end": en, "text": text, "tokens": toks,
                    "gap_before": (st - prev_end) if prev_end is not None else 9.0})
        prev_end = en
    return out


def topic_boundaries(sentences: List[Dict[str, Any]], k: int = 4) -> List[Dict[str, float]]:
    """Find where the conversation changes topic (TextTiling-style, no model needed).

    For each gap between sentences, compare the words used in the k sentences before with
    the k after. A sharp drop in shared words (plus a longer pause) means a new topic or a
    new story starts there. Returns [{"start": t_new, "prev_end": t, "strength": s}, ...].
    """
    n = len(sentences)
    if n < 2 * k + 1:
        return []
    from collections import Counter

    def bag(lo: int, hi: int) -> Counter:
        c: Counter = Counter()
        for snt in sentences[max(0, lo):min(n, hi)]:
            c.update(snt["tokens"])
        return c

    sims = np.zeros(n, dtype=np.float32)
    for i in range(1, n):
        a, b = bag(i - k, i), bag(i, i + k)
        if not a or not b:
            sims[i] = 0.0
            continue
        dot = sum(a[t] * b[t] for t in a if t in b)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        sims[i] = dot / (na * nb) if na and nb else 0.0

    depth = np.zeros(n, dtype=np.float32)
    for i in range(1, n):
        l = i
        while l - 1 >= 1 and sims[l - 1] >= sims[l]:
            l -= 1
        r = i
        while r + 1 < n and sims[r + 1] >= sims[r]:
            r += 1
        depth[i] = (sims[l] - sims[i]) + (sims[r] - sims[i])
        depth[i] += 0.25 * min(1.0, max(0.0, sentences[i]["gap_before"] - 0.6) / 2.0)

    vals = depth[1:]
    thr = float(vals.mean() + 0.5 * vals.std()) if len(vals) else 1.0
    out = []
    for i in range(1, n):
        is_local_max = depth[i] >= depth[i - 1] and (i + 1 >= n or depth[i] >= depth[i + 1])
        if depth[i] >= thr and is_local_max:
            out.append({"start": sentences[i]["start"], "prev_end": sentences[i - 1]["end"],
                        "strength": round(float(depth[i]), 3)})
    return out


def snap_moment_boundaries(
    peak_time: float,
    stream_duration: float,
    full_transcript_data: Optional[Dict[str, Any]] = None,
    min_length: float = 15.0,
    max_length: float = 60.0,
    max_lead: float = 5.0,
    max_trail: float = 5.0,
    before: float = 20.0,
    after: float = 20.0,
    hype_curve: Optional[np.ndarray] = None,
    topics: Optional[List[Dict[str, float]]] = None,
) -> Tuple[float, float]:
    """Choose clip boundaries around a hype peak so the clip starts and ends cleanly.

    - Start: chat and loudness peak AFTER the thing that caused them, so the clip leads with
      setup (~`before` seconds) and begins where a sentence begins or after a pause, never
      mid-word.
    - End: after the reaction dies down (hype curve back under 30% of the peak), at the end
      of a sentence or at a pause, with a little room for the laugh/breath.
    """
    words = (full_transcript_data or {}).get("words", []) or []

    # 1. Where does the reaction settle?
    settle = min(stream_duration, peak_time + after)
    if hype_curve is not None and len(hype_curve) > 0:
        p = int(min(len(hype_curve) - 1, max(0, peak_time)))
        lo, hi = max(0, p - 3), min(len(hype_curve), p + 4)
        peak_val = float(np.max(hype_curve[lo:hi])) if hi > lo else 0.0
        if peak_val > 0:
            quiet = 0
            limit = int(min(len(hype_curve), peak_time + after))
            for sec in range(p + 4, limit):
                quiet = quiet + 1 if hype_curve[sec] < 0.30 * peak_val else 0
                if quiet >= 3:
                    settle = float(sec)
                    break
    settle = max(settle, min(stream_duration, peak_time + 5.0))

    # 2. Start: closest natural start to (peak - before), within a window
    want_start = max(0.0, peak_time - before)
    starts, _ = _sentence_marks(words, max(0.0, want_start - 8.0), max(0.0, peak_time - 4.0))
    if starts:
        start = min(starts, key=lambda c: abs(c[0] - want_start) - 0.8 * c[1])[0]
    else:
        start = want_start

    # 3. End: first natural end after the reaction settles
    _, ends = _sentence_marks(words, max(peak_time + 3.0, settle - 3.0), min(stream_duration, peak_time + after + 8.0))
    if ends:
        after_settle = [c for c in ends if c[0] >= settle - 1.0]
        end = (after_settle[0] if after_settle else max(ends, key=lambda c: c[1]))[0]
    else:
        end = settle + 1.0

    # 3b. Context: if we know where topics/stories change, start where the current story
    # starts and stop where it is over, so a viewer gets the whole thing.
    if topics:
        setup_min = 8.0
        starts_t = [tb for tb in topics
                    if peak_time - (max_length - 8.0) <= tb["start"] <= peak_time - setup_min]
        if starts_t:
            tb = max(starts_t, key=lambda x: x["start"])  # start of the story the peak is in
            start = max(0.0, tb["start"] - 0.25)
        ends_t = [tb for tb in topics if max(peak_time + 5.0, settle - 8.0) <= tb["prev_end"] <= start + max_length]
        if ends_t:
            tb = min(ends_t, key=lambda x: x["prev_end"])  # the story wraps up here
            end = min(tb["prev_end"] + 0.6, tb["start"] - 0.05)

    start = max(0.0, start)
    end = min(stream_duration, end)

    # 4. Length limits, still landing on natural points where possible
    if end - start < min_length:
        _, more = _sentence_marks(words, start + min_length, min(stream_duration, start + max_length))
        end = more[0][0] if more else min(stream_duration, start + min_length)
        if end - start < min_length:
            start = max(0.0, end - min_length)
    if end - start > max_length:
        # Keep the payoff: cut setup from the front, at a sentence start if there is one
        later, _ = _sentence_marks(words, end - max_length, peak_time - 2.0)
        start = later[0][0] if later else end - max_length
        if end - start > max_length:
            start = end - max_length

    return round(start, 2), round(end, 2)


def dedupe_moments(
    new_moments: List[Dict[str, Any]],
    vod_id: str,
    overlap_threshold: float = 0.50,
    downloads_dir: str = "downloads",
    persist: bool = False,
) -> List[Dict[str, Any]]:
    """Deduplicate moments against previously clipped moments stored in downloads/<vod_id>/clipped_moments.json.

    Never output a moment overlapping > 50% with an existing or higher-ranked moment.
    """
    clipped_file = os.path.join(downloads_dir, vod_id, "clipped_moments.json")
    os.makedirs(os.path.dirname(clipped_file), exist_ok=True)

    existing_ranges: List[Tuple[float, float]] = []
    if os.path.isfile(clipped_file):
        try:
            with open(clipped_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    existing_ranges.append((item["start"], item["end"]))
        except Exception as e:
            logger.warning(f"Failed to read existing clipped moments: {e}")

    accepted_moments: List[Dict[str, Any]] = []

    for m in new_moments:
        st = m["start"]
        et = m["end"]
        dur = max(0.1, et - st)

        # Check overlap against existing and previously accepted in this batch
        overlapping = False
        all_checks = existing_ranges + [(am["start"], am["end"]) for am in accepted_moments]

        for prev_s, prev_e in all_checks:
            prev_dur = max(0.1, prev_e - prev_s)
            overlap_start = max(st, prev_s)
            overlap_end = min(et, prev_e)
            if overlap_end > overlap_start:
                overlap_dur = overlap_end - overlap_start
                # Overlap ratio relative to the shorter moment
                ratio = overlap_dur / min(dur, prev_dur)
                if ratio > overlap_threshold:
                    overlapping = True
                    break

        if not overlapping:
            accepted_moments.append(m)

    # Detection must NOT write here: a moment only counts as "clipped" once it has been
    # rendered (see record_clipped_moment). Otherwise re-running detection returns nothing.
    if not persist:
        return accepted_moments

    updated_records = []
    for s, e in existing_ranges:
        updated_records.append({"start": s, "end": e})
    for m in accepted_moments:
        updated_records.append({"start": m["start"], "end": m["end"], "score": m.get("score", 0.0)})

    try:
        with open(clipped_file, "w", encoding="utf-8") as f:
            json.dump(updated_records, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not persist clipped moments to {clipped_file}: {e}")

    return accepted_moments


def record_clipped_moment(vod_id: str, start: float, end: float, score: float = 0.0,
                          downloads_dir: str = "downloads") -> None:
    """Remember a moment that was actually rendered, so later runs never clip it twice."""
    clipped_file = os.path.join(downloads_dir, vod_id, "clipped_moments.json")
    os.makedirs(os.path.dirname(clipped_file), exist_ok=True)
    records: List[Dict[str, Any]] = []
    if os.path.isfile(clipped_file):
        try:
            with open(clipped_file, "r", encoding="utf-8") as f:
                records = json.load(f) or []
        except Exception:
            records = []
    for r in records:
        if abs(r.get("start", -1) - start) < 1.0 and abs(r.get("end", -1) - end) < 1.0:
            return
    records.append({"start": start, "end": end, "score": score})
    try:
        with open(clipped_file, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not persist clipped moment: {e}")


def save_hype_curve(vod_id: str, composite: np.ndarray, breakdown: Dict[str, np.ndarray],
                    downloads_dir: str = "downloads", step: int = 5) -> Optional[str]:
    """Save a downsampled hype curve (max per `step` seconds) for the review UI."""
    try:
        out_dir = os.path.join(downloads_dir, vod_id)
        os.makedirs(out_dir, exist_ok=True)

        def ds(arr: np.ndarray) -> List[float]:
            n = (len(arr) // step) * step
            if n == 0:
                return [round(float(x), 2) for x in arr]
            return [round(float(x), 2) for x in arr[:n].reshape(-1, step).max(axis=1)]

        path = os.path.join(out_dir, "hype_curve.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"step": step, "duration": int(len(composite)), "composite": ds(composite),
                       "signals": {k: ds(v) for k, v in breakdown.items() if np.any(v > 0.01)}}, f)
        return path
    except Exception as e:
        logger.warning(f"Could not save hype curve: {e}")
        return None
