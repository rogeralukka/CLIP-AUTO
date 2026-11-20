"""Cloud LLM Cascade Engine for CLIP AUTO.

Orchestrates multi-provider cloud fallback:
  Tier 1: Groq Cloud (Blazing-fast ~0.8s, Qwen 27B / GPT-OSS 120B)
  Tier 2: NVIDIA NIM (Nemotron 3.5 / MiniMax M3.1 Flash)
  Tier 3: Google Gemini (Gemini 3.8 Flash / Gemini Flash Latest)
  Tier 4: Local Ollama (RTX GPU fallback when offline or all rate-limited)
"""
import os
import re
import json
import time
import logging
from typing import Optional, Dict, Any, List, Tuple
import requests

try:
    from dotenv import load_dotenv
    # Load .env from project root
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.isfile(env_path):
        load_dotenv(env_path)
except Exception:
    pass

logger = logging.getLogger("clipper.llm_cascade")

# Telemetry tracking
_last_active_provider: str = "ollama"
_last_latency_s: float = 0.0
_provider_fail_counts: Dict[str, int] = {}


def _get_api_key(name: str) -> str:
    """Retrieve API key from env or config."""
    key = os.environ.get(name, "").strip()
    if key:
        return key
    # Fallback check in config/clipper.yaml or user_overrides
    try:
        from .config import load_config
        cfg = load_config().get("llm", {})
        return str(cfg.get("keys", {}).get(name.lower(), "")).strip()
    except Exception:
        return ""


def clean_json_response(raw_text: str) -> str:
    """Extract clean JSON substring from response."""
    if not raw_text:
        return ""
    text = raw_text.strip()
    # Strip markdown code blocks
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    # Find outer bracket or brace
    m = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def call_groq(prompt: str, system_prompt: Optional[str] = None, timeout: float = 12.0) -> Optional[str]:
    """Tier 1: Blazing fast Groq Cloud inference (< 1.0s)."""
    api_key = _get_api_key("GROQ_API_KEY")
    if not api_key:
        return None

    models = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    sys_content = system_prompt or "You are a world-class viral video judge and hook writer for TikTok, YouTube Shorts, and Reels. Output strict, valid JSON only."

    for model in models:
        try:
            t0 = time.time()
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "CLIP_AUTO/2.0"
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": sys_content},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                    "max_tokens": 1024
                },
                timeout=timeout
            )
            if resp.status_code == 200:
                res_data = resp.json()
                content = res_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    logger.info(f"Groq ({model}) answered in {time.time() - t0:.2f}s")
                    return clean_json_response(content)
            elif resp.status_code in (429, 503):
                logger.warning(f"Groq ({model}) busy/rate-limited (HTTP {resp.status_code}). Cascading to next...")
            else:
                logger.debug(f"Groq ({model}) returned {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            logger.debug(f"Groq ({model}) exception: {e}")
    return None


def call_nvidia(prompt: str, system_prompt: Optional[str] = None, timeout: float = 20.0) -> Optional[str]:
    """Tier 2: NVIDIA NIM (Nemotron 3.5 & High-IQ Cloud Reasoning)."""
    api_key = _get_api_key("NVIDIA_API_KEY")
    if not api_key:
        return None

    # Tested and reliable models on integrate.api.nvidia.com
    models = ["nvidia/nemotron-3.5-lightning-30b-a3b", "minimaxai/minimax-m3.1-flash", "google/gemma-4-31b-it"]
    sys_content = system_prompt or "You are a master viral video editor and content strategist. Output strict, valid JSON only."

    for model in models:
        try:
            t0 = time.time()
            resp = requests.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "CLIP_AUTO/2.0"
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": sys_content},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.2,
                    "max_tokens": 2048
                },
                timeout=timeout
            )
            if resp.status_code == 200:
                msg = resp.json().get("choices", [{}])[0].get("message", {})
                content = msg.get("content")
                if not content and msg.get("reasoning_content"):
                    # Extract any JSON embedded inside reasoning
                    cleaned = clean_json_response(msg.get("reasoning_content"))
                    if cleaned and (cleaned.startswith("{") or cleaned.startswith("[")):
                        content = cleaned
                if content:
                    logger.info(f"NVIDIA ({model}) answered in {time.time() - t0:.2f}s")
                    return clean_json_response(content)
            elif resp.status_code in (429, 503):
                logger.warning(f"NVIDIA ({model}) rate-limited (HTTP {resp.status_code}). Cascading to next...")
            else:
                logger.debug(f"NVIDIA ({model}) returned {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            logger.debug(f"NVIDIA ({model}) exception: {e}")
    return None


def call_gemini(prompt: str, system_prompt: Optional[str] = None, timeout: float = 15.0) -> Optional[str]:
    """Tier 3: Google Gemini (Deep Context & Slang Understanding)."""
    api_key = _get_api_key("GEMINI_API_KEY")
    if not api_key:
        return None

    models = ["gemini-3.8-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

    for model in models:
        try:
            t0 = time.time()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": full_prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "temperature": 0.2,
                        "maxOutputTokens": 1024
                    }
                },
                timeout=timeout
            )
            if resp.status_code == 200:
                res_json = resp.json()
                parts = res_json.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                if parts and "text" in parts[0]:
                    logger.info(f"Gemini ({model}) answered in {time.time() - t0:.2f}s")
                    return clean_json_response(parts[0]["text"])
            elif resp.status_code in (429, 503):
                logger.warning(f"Gemini ({model}) busy/quota (HTTP {resp.status_code}). Cascading...")
            else:
                logger.debug(f"Gemini ({model}) returned {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            logger.debug(f"Gemini ({model}) exception: {e}")
    return None


def call_local_ollama(
    prompt: str,
    endpoint: str = "http://localhost:11434",
    model: str = "qwen3:14b",
    timeout: float = 120.0
) -> Optional[str]:
    """Tier 4: Offline Local Ollama on RTX GPU."""
    try:
        t0 = time.time()
        url = f"{endpoint.rstrip('/')}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "think": False,
            "keep_alive": "-1",
            "options": {"temperature": 0.2, "num_ctx": 4096}
        }
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code == 200:
            res_json = resp.json()
            logger.info(f"Local Ollama ({model}) answered in {time.time() - t0:.2f}s")
            return clean_json_response(res_json.get("response", ""))
    except Exception as e:
        logger.warning(f"Local Ollama inference failed: {e}")
    return None


def call_llm_cascade(
    prompt: str,
    system_prompt: Optional[str] = None,
    endpoint: str = "http://localhost:11434",
    local_model: str = "qwen3:14b",
    timeout: float = 120.0,
    preferred_model: Optional[str] = None,
) -> Optional[str]:
    """Execute LLM call across cascading tiers until a high-quality answer is obtained.

    Respects user model preference (NVIDIA Nemotron, Groq, Gemini, or Local) with
    automatic failover so inference never stalls.
    """
    global _last_active_provider, _last_latency_s

    # Determine user preference
    pref = preferred_model
    if not pref:
        try:
            from .config import load_config
            pref = load_config().get("llm", {}).get("model", "")
        except Exception:
            pref = ""
    pref_str = str(pref or "").strip().lower()

    # Define tier executors
    def run_groq():
        if _get_api_key("GROQ_API_KEY"):
            t0 = time.time()
            res = call_groq(prompt, system_prompt=system_prompt, timeout=12.0)
            if res:
                return res, "groq (qwen 27b)", round(time.time() - t0, 2)
        return None, None, 0.0

    def run_nvidia():
        if _get_api_key("NVIDIA_API_KEY"):
            t0 = time.time()
            res = call_nvidia(prompt, system_prompt=system_prompt, timeout=20.0)
            if res:
                return res, "nvidia (nemotron)", round(time.time() - t0, 2)
        return None, None, 0.0

    def run_gemini():
        if _get_api_key("GEMINI_API_KEY"):
            t0 = time.time()
            res = call_gemini(prompt, system_prompt=system_prompt, timeout=15.0)
            if res:
                return res, "gemini (flash)", round(time.time() - t0, 2)
        return None, None, 0.0

    def run_ollama():
        t0 = time.time()
        res = call_local_ollama(prompt, endpoint=endpoint, model=local_model, timeout=timeout)
        if res:
            return res, f"ollama ({local_model})", round(time.time() - t0, 2)
        return None, None, 0.0

    # Determine execution order based on user preference
    if "nvidia" in pref_str or "nemotron" in pref_str:
        order = [run_nvidia, run_groq, run_gemini, run_ollama]
    elif "groq" in pref_str:
        order = [run_groq, run_nvidia, run_gemini, run_ollama]
    elif "gemini" in pref_str:
        order = [run_gemini, run_groq, run_nvidia, run_ollama]
    elif pref_str and not any(k in pref_str for k in ("auto", "cascade", "default")):
        # User specified local model (e.g. qwen3:14b)
        order = [run_ollama, run_groq, run_nvidia, run_gemini]
    else:
        # Default Auto Cascade (fastest available first)
        order = [run_groq, run_nvidia, run_gemini, run_ollama]

    for runner in order:
        res, prov_name, latency = runner()
        if res:
            _last_active_provider = prov_name
            _last_latency_s = latency
            return res

    logger.error("All LLM tiers in cascade failed or rate-limited.")
    return None


def get_llm_status(preferred_model: Optional[str] = None) -> Dict[str, Any]:
    """Return diagnostic status of all configured providers, reflecting user preference."""
    try:
        from .config import load_config
        pref = preferred_model or load_config().get("llm", {}).get("model", "")
    except Exception:
        pref = preferred_model or ""

    pref_lower = str(pref or "").lower()
    if "nvidia" in pref_lower or "nemotron" in pref_lower:
        active_display = "nvidia (nemotron)"
    elif "groq" in pref_lower:
        active_display = "groq (qwen 27b)"
    elif "gemini" in pref_lower:
        active_display = "gemini (flash)"
    elif pref and not any(k in pref_lower for k in ("auto", "cascade")):
        active_display = f"ollama ({pref})"
    else:
        active_display = _last_active_provider or "auto cascade"

    return {
        "selected_model": pref,
        "active_provider": active_display,
        "last_active_provider": _last_active_provider,
        "last_latency_s": _last_latency_s,
        "providers_configured": {
            "groq": bool(_get_api_key("GROQ_API_KEY")),
            "nvidia": bool(_get_api_key("NVIDIA_API_KEY")),
            "gemini": bool(_get_api_key("GEMINI_API_KEY")),
            "ollama": True
        }
    }
