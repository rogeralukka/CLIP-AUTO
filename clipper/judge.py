"""Local LLM Judge module via Ollama (Qwen 8B/7B) with VRAM unloading and strict JSON schema."""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .config import load_config
from .transcription import get_transcript_text_in_window, unload_whisper_model

logger = logging.getLogger("clipper.judge")


def is_ollama_available(endpoint: str = "http://localhost:11434", timeout: float = 2.0) -> Tuple[bool, List[str]]:
    """Check if Ollama server is running at endpoint and list pulled models."""
    tags_url = f"{endpoint.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(tags_url, headers={"User-Agent": "StreamClipper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8")
                data = json.loads(body)
                model_names = [m.get("name", "") for m in data.get("models", [])]
                return True, model_names
    except Exception:
        pass
    return False, []


def validate_judge_response(raw_text: str) -> Optional[Dict[str, Any]]:
    """Parse and validate strict JSON schema from LLM response.

    Schema:
    {
      "score": 0-10,
      "standalone": bool,
      "payoff_inside": bool,
      "hook_title": "<= 7 words, no clickbait lies, no spoilers",
      "reason": "one sentence"
    }
    """
    if not raw_text or not raw_text.strip():
        return None

    # Try extracting JSON block if model output wrapped in markdown
    text = raw_text.strip()
    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        text = json_match.group(0)

    try:
        data = json.loads(text)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    # Validate score
    raw_score = data.get("score")
    if raw_score is None:
        return None
    try:
        score_val = float(raw_score)
        score_val = max(0.0, min(10.0, score_val))
    except (ValueError, TypeError):
        return None

    standalone = bool(data.get("standalone", True))
    payoff_inside = bool(data.get("payoff_inside", True))

    hook_title = str(data.get("hook_title", "")).strip()
    # Enforce max 7 words
    words = hook_title.split()
    if len(words) > 7:
        hook_title = " ".join(words[:7])

    reason = str(data.get("reason", "")).strip()

    return {
        "score": round(score_val, 2),
        "standalone": standalone,
        "payoff_inside": payoff_inside,
        "hook_title": hook_title,
        "reason": reason,
    }


def find_ollama_exe() -> Optional[str]:
    """Where ollama is installed (PATH first, then the usual Windows install folder)."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
        if base:
            cand = os.path.join(base, "Programs", "Ollama", "ollama.exe") if base == os.environ.get("LOCALAPPDATA") \
                else os.path.join(base, "Ollama", "ollama.exe")
            if os.path.isfile(cand):
                return cand
    return None


def ensure_ollama_running(endpoint: str = "http://localhost:11434", wait_seconds: float = 20.0) -> bool:
    """Start the Ollama server in the background when it is not running (no window).

    Returns True when it answers. Never raises: no Ollama installed just means no AI judge."""
    ok, _ = is_ollama_available(endpoint)
    if ok:
        return True
    exe = find_ollama_exe()
    if not exe:
        logger.info("Ollama is not installed (or not on PATH); the AI judge stays off.")
        return False
    try:
        flags = 0
        if sys.platform == "win32":
            flags = 0x08000000 | 0x00000008   # CREATE_NO_WINDOW | DETACHED_PROCESS
        subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
        logger.info("Started Ollama in the background.")
    except Exception as e:
        logger.warning(f"Could not start Ollama: {e}")
        return False
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(0.5)
        if is_ollama_available(endpoint, timeout=1.0)[0]:
            return True
    return False


def unload_ollama_model(endpoint: str, model: str) -> None:
    """Free the GPU right away (Whisper and the video render need the VRAM)."""
    try:
        payload = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
        req = urllib.request.Request(f"{endpoint.rstrip('/')}/api/generate", data=payload,
                                     headers={"Content-Type": "application/json", "User-Agent": "StreamClipper/1.0"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass


def call_ollama_generate(
    prompt: str,
    endpoint: str = "http://localhost:11434",
    model: str = "qwen3:14b",
    timeout: float = 120.0,
) -> Optional[str]:
    """Call Ollama /api/generate (JSON output, thinking off, model kept loaded between calls).

    The model stays in VRAM for a couple of minutes so judging 30 moments is not 30 model loads;
    rank_moments_with_llm unloads it when it is done (12GB VRAM rule)."""
    gen_url = f"{endpoint.rstrip('/')}/api/generate"
    llm_cfg = load_config().get("llm", {})
    keep_alive_cfg = llm_cfg.get("keep_alive_seconds", 180)
    keep_alive = -1 if int(keep_alive_cfg) == -1 else f"{int(keep_alive_cfg)}s"

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "think": False,          # Qwen3 etc. would otherwise spend the answer on "thinking" text
        "keep_alive": keep_alive,
        "options": {
            "temperature": 0.2,
            "num_ctx": int(llm_cfg.get("num_ctx", 4096)),
        }
    }

    for attempt in (0, 1):
        try:
            data_bytes = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                gen_url,
                data=data_bytes,
                headers={"Content-Type": "application/json", "User-Agent": "StreamClipper/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    res_json = json.loads(resp.read().decode("utf-8"))
                    return res_json.get("response", "")
        except urllib.error.HTTPError as e:
            if e.code == 400 and attempt == 0 and "think" in payload:
                payload.pop("think")      # a model without a thinking mode refuses the flag
                continue
            logger.warning(f"Ollama inference failed ({e}).")
            return None
        except Exception as e:
            logger.warning(f"Ollama inference failed ({e}).")
            return None

    return None


def load_creator_few_shot_feedback() -> str:
    """Load creator ratings from data/ai_feedback.json to train Qwen on the user's taste."""
    feedback_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ai_feedback.json")
    if not os.path.isfile(feedback_file):
        return ""
    try:
        with open(feedback_file, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not data:
            return ""
        high_rated = [d for d in data if d.get("rating", 0) >= 4][:3]
        low_rated = [d for d in data if 1 <= d.get("rating", 0) <= 2][:3]
        if not high_rated and not low_rated:
            return ""
        lines = ["\nLEARNED CREATOR PREFERENCES (Rated by the creator):"]
        if high_rated:
            lines.append("Approved by Creator (High Virality, 4-5 stars):")
            for d in high_rated:
                t = d.get("hook_title") or d.get("filename")
                tag = f" [{d.get('tag')}]" if d.get('tag') else ""
                q = f' (Speech: "{d.get("transcript")[:75]}...")' if d.get("transcript") else ""
                lines.append(f'  - "{t}"{tag}{q}: High quality, strong standalone payoff.')
        if low_rated:
            lines.append("Disliked / Rejected by Creator (Poor / Hollow, 1-2 stars):")
            for d in low_rated:
                t = d.get("hook_title") or d.get("filename")
                tag = f" [{d.get('tag')}]" if d.get('tag') else ""
                q = f' (Speech: "{d.get("transcript")[:75]}...")' if d.get("transcript") else ""
                lines.append(f'  - "{t}"{tag}{q}: Hollow, lacks punchline, or cut awkwardly.')
        lines.append("Align your score and feedback strictly with the creator's demonstrated taste.\n")
        return "\n".join(lines)
    except Exception:
        return ""


def build_judge_prompt(
    start_sec: float,
    end_sec: float,
    transcript: str,
    chat_sample: List[str],
    signal_breakdown: Dict[str, float],
) -> str:
    """Build concise evaluation prompt for local LLM judge."""
    chat_str = "\n".join(f"- {m}" for m in chat_sample[:8]) if chat_sample else "No chat comments recorded."
    breakdown_str = json.dumps(signal_breakdown)
    creator_feedback = load_creator_few_shot_feedback()

    return f"""You are a professional viral short-form video editor judging stream clip candidates.
Evaluate this potential vertical short moment ({end_sec - start_sec:.1f}s duration).

RULES FOR JUDGING:
1. Judge harshly: would a stranger scrolling TikTok/Reels/Shorts stop within 2 seconds?
2. Does the clip make sense standalone with ZERO prior stream context?
3. Does the punchline, reaction, clutch, or payoff happen INSIDE this clip window?
4. Output a hook title (<= 7 words) that captures attention without fake clickbait or spoilers.
{creator_feedback}
INPUT DATA:
- Spoken Transcript:
"{transcript.strip() if transcript.strip() else '[No speech / gameplay sounds only]'}"

- Viewer Chat Sample:
{chat_str}

- Quantitative Signal Breakdown (robust z-scores):
{breakdown_str}

Respond ONLY with valid JSON following this schema exactly:
{{
  "score": <float 0.0 to 10.0>,
  "standalone": <true or false>,
  "payoff_inside": <true or false>,
  "hook_title": "<concise hook title <= 7 words>",
  "reason": "<one clear sentence explaining the score>"
}}"""


def judge_single_moment(
    moment: Dict[str, Any],
    transcript: str,
    chat_sample: List[str],
    endpoint: str = "http://localhost:11434",
    model: str = "qwen3:14b",
    timeout: float = 120.0,
    retry_count: int = 1,
) -> Optional[Dict[str, Any]]:
    """Send moment to Ollama judge, validating output and retrying once on bad JSON."""
    breakdown = moment.get("breakdown", {})
    prompt = build_judge_prompt(
        start_sec=moment["start"],
        end_sec=moment["end"],
        transcript=transcript,
        chat_sample=chat_sample,
        signal_breakdown=breakdown,
    )

    for attempt in range(retry_count + 1):
        raw_output = call_ollama_generate(prompt, endpoint=endpoint, model=model, timeout=timeout)
        if raw_output:
            validated = validate_judge_response(raw_output)
            if validated:
                return validated
        logger.info(f"LLM judge retry {attempt + 1} for moment at t={moment['start']}...")

    return None


def refine_boundaries_with_llm(
    moment: Dict[str, Any],
    full_transcript_data: Optional[Dict[str, Any]],
    endpoint: str,
    model: str,
    timeout: float,
    min_len: float = 15.0,
    max_len: float = 90.0,
) -> bool:
    """Ask the local model where the story around this moment starts and where it is over.

    The model sees numbered sentences with timestamps and picks the first and last sentence
    a viewer needs to understand what happened. Updates moment start/end in place; returns
    True if it changed them.
    """
    from .signals import build_sentences
    words = (full_transcript_data or {}).get("words") or []
    if not words:
        return False
    peak = float(moment.get("peak_second") or (moment["start"] + moment["end"]) / 2.0)
    lo, hi = peak - (max_len + 10.0), peak + 50.0
    near = [w for w in words if lo <= float(w["start"]) <= hi]
    sents = build_sentences(near)
    if len(sents) < 3:
        return False
    peak_idx = min(range(len(sents)), key=lambda i: 0 if sents[i]["start"] <= peak <= sents[i]["end"] + 1.0
                   else abs(sents[i]["start"] - peak))
    lines = "\n".join(f"[{i}] ({s['start'] - peak:+.0f}s) {s['text']}" for i, s in enumerate(sents))
    prompt = f"""You cut livestream clips into YouTube Shorts. Below is the transcript around a hype
moment (times are seconds relative to the moment, sentence [{peak_idx}] is where it peaks).

Pick the FIRST sentence a new viewer needs to understand what is going on (the start of this
story/topic, not mid-conversation), and the LAST sentence where this story is clearly over
(after the reaction, before a new topic starts). The clip must be {min_len:.0f}-{max_len:.0f}
seconds long and must contain sentence [{peak_idx}].

{lines}

Reply ONLY with JSON: {{"first": <int>, "last": <int>, "topic": "<what happens, max 10 words>", "complete": <true if the story is fully inside>}}"""
    raw = call_ollama_generate(prompt, endpoint=endpoint, model=model, timeout=timeout)
    if not raw:
        return False
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group(0) if m else raw)
        a, b = int(data["first"]), int(data["last"])
    except Exception:
        return False
    if not (0 <= a <= peak_idx <= b < len(sents)):
        return False
    st = max(0.0, sents[a]["start"] - 0.25)
    nxt = sents[b + 1]["start"] if b + 1 < len(sents) else sents[b]["end"] + 1.0
    en = min(sents[b]["end"] + 0.6, nxt - 0.05)
    if not (min_len - 1.0 <= en - st <= max_len + 1.0):
        return False
    moment["start"], moment["end"] = round(st, 2), round(en, 2)
    moment["topic"] = str(data.get("topic", ""))[:80]
    moment["story_complete"] = bool(data.get("complete", True))
    return True


def rank_moments_with_llm(
    moments: List[Dict[str, Any]],
    top_n: Optional[int] = None,
    full_transcript_data: Optional[Dict[str, Any]] = None,
    comments: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Phase 3: Re-rank top candidates using local LLM judge if Ollama is running and llm.enabled is True."""
    cfg = load_config()
    llm_cfg = cfg.get("llm", {})

    if not llm_cfg.get("enabled", False):
        logger.debug("LLM judge disabled in config; keeping hype-curve ranking.")
        return moments[:top_n] if top_n else moments

    endpoint = llm_cfg.get("endpoint", "http://localhost:11434")
    model_name = llm_cfg.get("model", "qwen3:14b")
    timeout = float(llm_cfg.get("timeout_seconds", 30))

    # Check Ollama connection (start it in the background when it is not running)
    ensure_ollama_running(endpoint)
    available, pulled_models = is_ollama_available(endpoint)
    if not available:
        logger.info(f"Ollama server not reachable at {endpoint}. Skipping LLM judge silently.")
        return moments[:top_n] if top_n else moments

    # Check model availability
    model_found = any(model_name in pm or pm.startswith(model_name.split(":")[0]) for pm in pulled_models)
    if not model_found and pulled_models:
        logger.warning(
            f"Configured model '{model_name}' is not pulled in Ollama (available: {pulled_models}). "
            f"Run: ollama pull {model_name}. Skipping LLM judge silently."
        )
        return moments[:top_n] if top_n else moments

    target_top = top_n if (top_n is not None and top_n > 0) else len(moments)
    multiplier = llm_cfg.get("candidate_multiplier", 3)
    num_candidates = min(len(moments), target_top * multiplier)
    candidates = moments[:num_candidates]

    logger.info(f"Evaluating top {len(candidates)} candidates with local LLM judge ({model_name})...")

    # 12GB VRAM Rule: Unload Whisper before running Ollama inference
    unload_whisper_model()

    weight_llm = float(llm_cfg.get("weight_llm", 0.50))
    weight_hype = float(llm_cfg.get("weight_hype", 0.50))

    try:
        for m in candidates:
            if llm_cfg.get("refine_boundaries", True):
                lim = m.get("len_limits") or (15.0, 90.0)
                try:
                    if refine_boundaries_with_llm(m, full_transcript_data, endpoint, model_name, timeout,
                                                  min_len=float(lim[0]), max_len=float(lim[1])):
                        logger.info(f"LLM set story bounds {m['start']:.1f}-{m['end']:.1f}: {m.get('topic', '')}")
                except Exception as e:
                    logger.debug(f"Boundary refine failed: {e}")
            st = m["start"]
            et = m["end"]

            # Transcript text
            t_text = ""
            if full_transcript_data:
                t_text = get_transcript_text_in_window(full_transcript_data, st, et)

            # Chat sample around peak
            chat_sample = []
            if comments:
                chat_sample = [
                    c.get("message", {}).get("body", "")
                    for c in comments
                    if c.get("content_offset_seconds") is not None and st - 2.0 <= c["content_offset_seconds"] <= et + 2.0
                    and c.get("message", {}).get("body", "").strip()
                ]

            # Call judge
            eval_res = judge_single_moment(
                moment=m,
                transcript=t_text,
                chat_sample=chat_sample,
                endpoint=endpoint,
                model=model_name,
                timeout=timeout,
                retry_count=llm_cfg.get("retry_count", 1),
            )

            if eval_res:
                m["llm"] = eval_res
                m["hook_title"] = eval_res.get("hook_title", "")
                m["llm_score"] = eval_res.get("score", 0.0)
                m["llm_reason"] = eval_res.get("reason", "")

                # Blend scores
                # Normalized hype score (scale 0-10)
                norm_hype = min(10.0, max(0.0, float(m.get("score", 0.0)) * 2.0))
                blended = weight_llm * eval_res["score"] + weight_hype * norm_hype
                # A clip that needs earlier context, or stops before the payoff, ranks lower
                if not eval_res.get("standalone", True):
                    blended -= 2.0
                if not eval_res.get("payoff_inside", True):
                    blended -= 1.5
                m["blended_score"] = round(blended, 2)
            else:
                # Fallback to pure hype score
                m["blended_score"] = round(float(m.get("score", 0.0)), 2)
                m["llm"] = None
    finally:
        unload_ollama_model(endpoint, model_name)

    # Sort candidates by blended score descending
    candidates.sort(key=lambda m: -m.get("blended_score", m.get("score", 0.0)))
    final_selected = candidates[:target_top]
    logger.info(f"LLM judging complete. Selected top {len(final_selected)} moments.")
    return final_selected
