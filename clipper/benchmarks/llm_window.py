"""Measure the H8 (story/roles) LLM pass on a REAL 3-minute window of a stream.

Replaces the estimate in docs/models.md ("~35 tok/s, ~1.5 s per chunk") with measured numbers.

    python -m clipper.benchmarks.llm_window <audio-or-video> --start 01:00:00 --minutes 3 \
        [--model qwen3:14b] [--endpoint http://localhost:11434]

What it does, all on this machine:
  1. cuts the window with ffmpeg and transcribes it with faster-whisper (large-v3-turbo),
  2. sends an H8-style prompt (tag each utterance's role + group into scenes) to Ollama,
  3. records Ollama's own timings (prompt tokens, output tokens, tokens/s, total time)
     and the model's VRAM from /api/ps,
  4. writes data/learning/reports/llm_window_<model>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request


def _hms(s: str) -> float:
    parts = [float(p) for p in s.split(":")]
    v = 0.0
    for p in parts:
        v = v * 60 + p
    return v


def _post(url: str, payload: dict, timeout: float = 600.0) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def transcribe_window(src: str, start: float, minutes: float):
    from faster_whisper import WhisperModel
    tmp = tempfile.mkdtemp()
    wav = os.path.join(tmp, "win.wav")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(start), "-t", str(minutes * 60),
                    "-i", src, "-ac", "1", "-ar", "16000", wav], check=True)
    model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
    t = time.perf_counter()
    segs, _ = model.transcribe(wav, vad_filter=True)
    utts = [{"i": i, "start": round(start + s.start, 2), "end": round(start + s.end, 2),
             "text": s.text.strip()} for i, s in enumerate(segs) if s.text.strip()]
    return utts, time.perf_counter() - t


PROMPT = """You label a livestream transcript. Use ONLY what is written; do not invent events.
For every utterance give a role: setup | question | answer | joke | punchline | reaction | callback | complaint | announcement | filler.
Then group utterances into scenes (consecutive, same topic). Each scene: start_i, end_i, one-line summary.
Return JSON: {{"roles": [{{"i": 0, "role": "..."}}], "scenes": [{{"start_i": 0, "end_i": 5, "summary": "..."}}]}}

Utterances:
{lines}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--start", default="00:00:00")
    ap.add_argument("--minutes", type=float, default=3.0)
    ap.add_argument("--model", default="qwen3:14b")
    ap.add_argument("--endpoint", default="http://localhost:11434")
    a = ap.parse_args()

    start = _hms(a.start)
    utts, t_asr = transcribe_window(a.src, start, a.minutes)
    try:  # free the whisper GPU memory before the LLM loads
        import gc, torch
        gc.collect(); torch.cuda.empty_cache()
    except Exception:
        pass
    lines = "\n".join(f"[{u['i']}] {u['text']}" for u in utts)
    payload = {"model": a.model, "prompt": PROMPT.format(lines=lines), "stream": False,
               "format": "json", "think": False, "keep_alive": "5m",
               "options": {"temperature": 0.2, "num_ctx": 8192}}

    t = time.perf_counter()
    res = _post(f"{a.endpoint.rstrip('/')}/api/generate", payload)
    wall = time.perf_counter() - t
    vram = None
    try:
        for m in _get(f"{a.endpoint.rstrip('/')}/api/ps").get("models", []):
            if m.get("name", "").startswith(a.model.split(":")[0]):
                vram = round(m.get("size_vram", 0) / 2**20, 1)
    except Exception:
        pass

    ev, ed = res.get("eval_count", 0), res.get("eval_duration", 0) / 1e9
    pc, pd = res.get("prompt_eval_count", 0), res.get("prompt_eval_duration", 0) / 1e9
    ok = True
    try:
        parsed = json.loads(res.get("response", "{}"))
        n_roles = len(parsed.get("roles", []))
        n_scenes = len(parsed.get("scenes", []))
    except Exception:
        ok, n_roles, n_scenes = False, 0, 0

    report = {
        "source": a.src, "window_start_s": start, "window_minutes": a.minutes, "model": a.model,
        "utterances": len(utts), "asr_seconds": round(t_asr, 2),
        "prompt_tokens": pc, "prompt_seconds": round(pd, 2),
        "output_tokens": ev, "output_seconds": round(ed, 2),
        "output_tokens_per_s": round(ev / ed, 1) if ed else None,
        "llm_wall_seconds": round(wall, 2),
        "load_seconds": round(res.get("load_duration", 0) / 1e9, 2),
        "model_vram_mb": vram,
        "json_valid": ok, "roles_returned": n_roles, "scenes_returned": n_scenes,
        "all_utterances_labelled": n_roles >= len(utts),
    }
    os.makedirs("data/learning/reports", exist_ok=True)
    out = f"data/learning/reports/llm_window_{a.model.replace(':', '_')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
