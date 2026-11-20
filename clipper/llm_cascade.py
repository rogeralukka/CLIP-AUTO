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


def call_nvidia(prompt: str, system_prompt: Optional[str] = None, timeout: float = 15.0) -> Optional[str]:
    """Tier 2: NVIDIA NIM (Nemotron & High-IQ Cloud Reasoning)."""
    api_key = _get_api_key("NVIDIA_API_KEY")
    if not api_key:
        return None

    # Tested and reliable models on integrate.api.nvidia.com
    models = ["minimaxai/minimax-m3.1-flash", "nvidia/nemotron-3.5-lightning-30b-a3b", "google/gemma-4-31b-it"]
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
                    "max_tokens": 1024
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
    timeout: float = 120.0
) -> Optional[str]:
    """Execute LLM call across cascading tiers until a high-quality answer is obtained.

    Order:
      1. Groq Cloud (~0.8s, Qwen 27B)
      2. NVIDIA NIM (~5s, MiniMax Flash / Nemotron)
      3. Google Gemini (~6s, Gemini Flash)
      4. Local Ollama (Offline / GPU fallback)
    """
    global _last_active_provider, _last_latency_s
    t_start = time.time()

    # 1. Try Groq Cloud
    if _get_api_key("GROQ_API_KEY"):
        t0 = time.time()
        res = call_groq(prompt, system_prompt=system_prompt, timeout=12.0)
        if res:
            _last_active_provider = "groq (qwen 27b)"
            _last_latency_s = round(time.time() - t0, 2)
            return res

    # 2. Try NVIDIA NIM
    if _get_api_key("NVIDIA_API_KEY"):
        t0 = time.time()
        res = call_nvidia(prompt, system_prompt=system_prompt, timeout=15.0)
        if res:
            _last_active_provider = "nvidia (nemotron/minimax)"
            _last_latency_s = round(time.time() - t0, 2)
            return res

    # 3. Try Google Gemini
    if _get_api_key("GEMINI_API_KEY"):
        t0 = time.time()
        res = call_gemini(prompt, system_prompt=system_prompt, timeout=15.0)
        if res:
            _last_active_provider = "gemini (flash)"
            _last_latency_s = round(time.time() - t0, 2)
            return res

    # 4. Fallback to Local Ollama
    t0 = time.time()
    res = call_local_ollama(prompt, endpoint=endpoint, model=local_model, timeout=timeout)
    if res:
        _last_active_provider = f"ollama ({local_model})"
        _last_latency_s = round(time.time() - t0, 2)
        return res

    logger.error("All LLM tiers in cascade failed or rate-limited.")
    return None


def get_llm_status() -> Dict[str, Any]:
    """Return diagnostic status of all configured providers."""
    return {
        "active_provider": _last_active_provider,
        "last_latency_s": _last_latency_s,
        "providers_configured": {
            "groq": bool(_get_api_key("GROQ_API_KEY")),
            "nvidia": bool(_get_api_key("NVIDIA_API_KEY")),
            "gemini": bool(_get_api_key("GEMINI_API_KEY")),
            "ollama": True
        }
    }
