"""CLIP AUTO studio: local web UI backend (FastAPI).

Run:  .\\.venv\\Scripts\\python.exe server.py      then open http://127.0.0.1:8765

Wraps the existing pipeline/queue. Nothing here uploads anything anywhere; it only
serves files on your own machine to your own browser (bound to 127.0.0.1).
"""
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

from clipper.bounties import (  # noqa: E402
    delete_bounty, get_bounty, import_bounty_as_campaign,
    link_bounty_account, load_bounties, upsert_bounty,
)
from clipper.campaigns import delete_campaign, list_campaigns, load_campaign, save_campaign  # noqa: E402
from clipper.config import load_config, save_user_override  # noqa: E402
from clipper.judge import ensure_ollama_running, is_ollama_available  # noqa: E402
from clipper.pipeline import rerender_clip  # noqa: E402
from clipper.queue import (  # noqa: E402
    add_job, cancel_job, clear_finished_jobs, confirm_render_moments, delete_job,
    ensure_worker_running, load_jobs, mutate_job, pause_job, resume_job,
)
from clipper.social import (  # noqa: E402
    LOGIN_LINKS, PLATFORM_LINKS, PLATFORMS, list_chrome_profiles, open_in_profile, calculate_next_slot, connect_youtube, connection_state, delete_account,
    delete_schedule_entry, disconnect, get_account, get_daily_scheduled_count, has_client_secret, load_accounts,
    mark_platform, queue_view, retry_platform, save_client_secret, save_credentials, schedule_clip_to_account,
    start_due_loop, upsert_account,
)

app = FastAPI(title="CLIP AUTO Studio")
MEDIA_DIRS = [os.path.join(ROOT, d) for d in ("clips", "exports", "downloads", "campaigns")]
_rerender_lock = threading.Lock()
_live_sessions: Dict[str, Dict[str, Any]] = {}
_ollama_thread: Optional[threading.Thread] = None


def _start_ollama_in_background():
    """Start Ollama in a background thread if it is not already running."""
    global _ollama_thread
    if _ollama_thread is not None and _ollama_thread.is_alive():
        return
    cfg = load_config()
    endpoint = cfg.get("llm", {}).get("endpoint", "http://localhost:11434")
    _ollama_thread = threading.Thread(target=ensure_ollama_running, args=(endpoint,), daemon=True, name="OllamaStarter")
    _ollama_thread.start()


ACTIVE_SESSIONS_FILE = os.path.join(ROOT, "downloads", "live_buffer", "active_sessions.json")


def _save_active_sessions():
    try:
        os.makedirs(os.path.dirname(ACTIVE_SESSIONS_FILE), exist_ok=True)
        active_list = []
        for sid, s in _live_sessions.items():
            c = s.get("clipper")
            active_list.append({
                "sid": sid,
                "url": s.get("url"),
                "started": s.get("started"),
                "quality": getattr(c, "quality", "data_saver"),
                "preset": getattr(c, "preset_name", "classic_yellow"),
                "campaign_id": getattr(c, "campaign", {}).get("id") if getattr(c, "campaign", None) else None,
                "sensitivity": getattr(c, "sensitivity", 2.0),
            })
        with open(ACTIVE_SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(active_list, f, indent=2)
    except Exception:
        pass


def _restore_active_sessions():
    from clipper.live import LiveStreamClipper
    restored_urls = set()

    # 1. Restore from ACTIVE_SESSIONS_FILE
    if os.path.isfile(ACTIVE_SESSIONS_FILE):
        try:
            with open(ACTIVE_SESSIONS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for item in saved:
                url = item.get("url")
                if not url or url in restored_urls:
                    continue
                camp = load_campaign(item.get("campaign_id")) if item.get("campaign_id") else None
                lc = LiveStreamClipper(
                    stream_url=url,
                    quality=item.get("quality"),
                    preset_name=item.get("preset") or "classic_yellow",
                    campaign=camp,
                    sensitivity=float(item.get("sensitivity") or 2.0),
                )
                t = None
                if not lc.is_paused:
                    t = threading.Thread(target=lc.run_loop, daemon=True, name=f"live-{lc.stream_id}")
                    t.start()
                sid = item.get("sid") or f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_]', '', lc.stream_id)[:10]}"
                _live_sessions[sid] = {"url": url, "clipper": lc, "thread": t, "started": item.get("started", time.strftime("%H:%M"))}
                restored_urls.add(url)
        except Exception:
            pass

    # 2. Discover any existing buffered streams with state.json
    for state_path in glob.glob(os.path.join(ROOT, "downloads", "live_buffer", "*", "state.json")):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                st_data = json.load(f)
            url = st_data.get("stream_url")
            if url and url not in restored_urls:
                lc = LiveStreamClipper(stream_url=url, quality=st_data.get("quality"))
                t = None
                if not lc.is_paused:
                    t = threading.Thread(target=lc.run_loop, daemon=True, name=f"live-{lc.stream_id}")
                    t.start()
                sid = f"saved_{re.sub(r'[^a-zA-Z0-9_]', '', lc.stream_id)[:10]}"
                _live_sessions[sid] = {"url": url, "clipper": lc, "thread": t, "started": time.strftime("%H:%M")}
                restored_urls.add(url)
        except Exception:
            pass


@app.on_event("startup")
def _on_startup():
    ensure_worker_running()
    start_due_loop()          # publishes Instagram posts when their time comes (while Studio runs)
    _start_ollama_in_background()
    _restore_active_sessions()


# ----------------------------------------------------------------- helpers
def _job(job_id: str) -> Dict[str, Any]:
    job = load_jobs().get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def _gpu_status() -> Dict[str, Any]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
        name, used, total, util = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        return {"ok": True, "name": name, "mem_used": int(used), "mem_total": int(total), "util": int(util)}
    except Exception:
        return {"ok": False}


# ----------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(ROOT, "web", "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    v = int(time.time())
    html = html.replace('/static/app.js', f'/static/app.js?v={v}')
    html = html.replace('/static/style.css', f'/static/style.css?v={v}')
    return HTMLResponse(content=html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })


app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "web")), name="static")


@app.get("/media")
def media(path: str):
    """Serve a local clip/audio file to the browser (only from the project's own folders)."""
    full = os.path.abspath(path if os.path.isabs(path) else os.path.join(ROOT, path))
    if not any(full.startswith(d + os.sep) for d in MEDIA_DIRS) or not os.path.isfile(full):
        raise HTTPException(404, "Not found")
    return FileResponse(full)


# ----------------------------------------------------------------- system
@app.get("/api/system")
def system():
    cfg = load_config()
    llm = cfg.get("llm", {})
    endpoint = llm.get("endpoint", "http://localhost:11434")
    ok, models = is_ollama_available(endpoint)
    if not ok:
        _start_ollama_in_background()
    presets = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob("presets/*.json"))
    from clipper.llm_cascade import get_llm_status
    cascade_status = get_llm_status()
    return {
        "gpu": _gpu_status(),
        "ollama": {"running": ok, "models": models},
        "llm": {"enabled": bool(llm.get("enabled")), "model": llm.get("model")},
        "cascade": cascade_status,
        "whisper_model": cfg.get("audio", {}).get("whisper_model", "large-v3"),
        "presets": presets,
        "polish": {"progress_bar": cfg.get("polish", {}).get("progress_bar", True)},
        "gameplay_aspect": cfg.get("facecam", {}).get("gameplay_aspect", "16:9"),
        "default_sensitivity": cfg.get("signals", {}).get("default_sensitivity", "Medium"),
        "live_sensitivity": cfg.get("signals", {}).get("live_sensitivity", 2.2),
    }


class SettingsIn(BaseModel):
    llm_enabled: Optional[bool] = None
    llm_model: Optional[str] = None
    whisper_model: Optional[str] = None
    progress_bar: Optional[bool] = None
    gameplay_aspect: Optional[str] = None
    default_sensitivity: Optional[str] = None
    live_sensitivity: Optional[float] = None


@app.post("/api/settings")
def settings(s: SettingsIn):
    if s.llm_enabled is not None or s.llm_model:
        vals = {}
        if s.llm_enabled is not None:
            vals["enabled"] = s.llm_enabled
        if s.llm_model:
            vals["model"] = s.llm_model
        save_user_override("llm", vals)
    if s.whisper_model:
        save_user_override("audio", {"whisper_model": s.whisper_model})
    if s.progress_bar is not None:
        save_user_override("polish", {"progress_bar": s.progress_bar})
    if s.gameplay_aspect:
        save_user_override("facecam", {"gameplay_aspect": s.gameplay_aspect})
    if s.default_sensitivity:
        save_user_override("signals", {"default_sensitivity": s.default_sensitivity})
    if s.live_sensitivity is not None:
        save_user_override("signals", {"live_sensitivity": float(s.live_sensitivity)})
        for v in _live_sessions.values():
            c = v.get("clipper")
            if c and hasattr(c, "update_sensitivity"):
                c.update_sensitivity(float(s.live_sensitivity))
    return system()


# ----------------------------------------------------------------- jobs
class NewJobs(BaseModel):
    links: List[str]
    campaign_id: Optional[str] = None
    quality: str = "1080p"
    top: Optional[int] = None
    sensitivity: float = 2.0
    review: bool = True
    preset: Optional[str] = None
    cpu: bool = False
    layout: str = "auto"


@app.get("/api/jobs")
def jobs():
    js = list(load_jobs().values())
    js.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    try:
        assigned_map = {}
        for p in queue_view():
            cp = p.get("clip_path")
            if p.get("status") == "FAILED":
                continue
            if cp:
                norm = os.path.normpath(os.path.abspath(cp)).lower()
                assigned_map[norm] = p.get("account_name") or p.get("account_id")
        for j in js:
            for c in j.get("clips", []):
                p = c.get("path")
                if p:
                    norm_p = os.path.normpath(os.path.abspath(p)).lower()
                    c["assigned_account"] = assigned_map.get(norm_p)
    except Exception:
        pass
    return [j for j in js if not j.get("is_dev_test")]


@app.post("/api/jobs")
def create_jobs(body: NewJobs):
    ids = []
    for raw in body.links:
        link = raw.strip()
        if not link:
            continue
        ids.append(add_job(
            vod_input=link,
            campaign_id=body.campaign_id or None,
            quality=body.quality,
            mode="MANUAL" if body.top else "AUTO",
            top=body.top,
            z_thresh=body.sensitivity,
            preset=body.preset or None,
            use_cpu=body.cpu,
            auto_render=not body.review,
            layout=body.layout if body.layout in ("auto", "split", "duo", "gameplay", "face", "irl") else "auto",
        ))
    if not ids:
        raise HTTPException(400, "Paste at least one link")
    return {"ids": ids}


@app.post("/api/jobs/{job_id}/do/{action}")
def job_action(job_id: str, action: str):
    _job(job_id)
    if action == "pause":
        pause_job(job_id)
    elif action == "resume":
        resume_job(job_id)
    elif action == "cancel":
        cancel_job(job_id)
    else:
        raise HTTPException(400, "Unknown action")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def remove_job(job_id: str):
    delete_job(job_id)
    return {"ok": True}


@app.post("/api/jobs-clear-finished")
def clear_finished():
    clear_finished_jobs()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/curve")
def curve(job_id: str):
    job = _job(job_id)
    moms = job.get("detected_moments") or []
    vod_id = (moms[0].get("vod_id") if moms else None)
    path = os.path.join("downloads", vod_id or "", "hype_curve.json")
    if not vod_id or not os.path.isfile(path):
        return {"composite": [], "signals": {}, "step": 5}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class Confirm(BaseModel):
    selected: List[int]
    moments: List[Dict[str, Any]]


@app.post("/api/jobs/{job_id}/confirm")
def confirm(job_id: str, body: Confirm):
    _job(job_id)
    confirm_render_moments(job_id, body.selected, updated_moments=body.moments or None)
    return {"ok": True}


# ----------------------------------------------------------------- clips
class ClipStatus(BaseModel):
    status: str  # KEPT | REJECTED | PENDING


@app.post("/api/jobs/{job_id}/clips/{idx}/status")
def clip_status(job_id: str, idx: int, body: ClipStatus):
    def _set(j):
        j["clips"][idx]["status"] = body.status
    mutate_job(job_id, _set)
    return {"ok": True}


class Rerender(BaseModel):
    layout: Optional[str] = None
    hook_title: Optional[str] = None
    preset: Optional[str] = None


def _do_rerender(job_id: str, idx: int, body: Rerender):
    with _rerender_lock:
        job = load_jobs().get(job_id, {})
        clip = job["clips"][idx]
        campaign = job.get("campaign") or None
        try:
            new_clip = rerender_clip(clip, layout=body.layout, hook_title=body.hook_title,
                                     preset_name=body.preset or job.get("preset"), campaign=campaign,
                                     overlay=(campaign or {}).get("overlay"), cpu=job.get("use_cpu", False))
            new_clip["status"] = clip.get("status", "PENDING")
            new_clip.pop("busy", None)
        except Exception as e:
            new_clip = dict(clip, busy=False, error=str(e))

        def _set(j):
            j["clips"][idx] = new_clip
        mutate_job(job_id, _set)


@app.post("/api/jobs/{job_id}/clips/{idx}/rerender")
def clip_rerender(job_id: str, idx: int, body: Rerender, bg: BackgroundTasks):
    job = _job(job_id)
    if idx >= len(job.get("clips", [])):
        raise HTTPException(404, "Clip not found")

    def _busy(j):
        j["clips"][idx]["busy"] = True
        j["clips"][idx].pop("error", None)
    mutate_job(job_id, _busy)
    bg.add_task(_do_rerender, job_id, idx, body)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/export")
def export_kept(job_id: str):
    job = _job(job_id)
    camp = (job.get("campaign") or {}).get("id") or "general"
    out_dir = os.path.join(ROOT, "exports", camp, time.strftime("%Y-%m-%d"))
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for c in job.get("clips", []):
        if c.get("status") != "KEPT" or not os.path.isfile(c.get("path", "")):
            continue
        base = f"{job_id}_{os.path.splitext(os.path.basename(c['path']))[0]}"
        shutil.copy2(c["path"], os.path.join(out_dir, base + ".mp4"))
        p = c.get("platforms") or {}
        with open(os.path.join(out_dir, base + "_captions.txt"), "w", encoding="utf-8") as f:
            f.write(f"HOOK: {c.get('hook_title') or ''}\n\n")
            for k, v in p.items():
                f.write(f"--- {k} ---\n{v}\n\n")
        n += 1
    return {"exported": n, "folder": out_dir}


@app.post("/api/open-folder")
def open_folder(body: Dict[str, str]):
    """Open a folder in Windows Explorer (local convenience)."""
    path = os.path.abspath(body.get("path", ""))
    if os.path.isdir(path) and path.startswith(ROOT) and hasattr(os, "startfile"):
        os.startfile(path)  # type: ignore[attr-defined]
    return {"ok": True}


# ----------------------------------------------------------------- campaigns
@app.get("/api/campaigns")
def campaigns():
    return list_campaigns()


@app.post("/api/campaigns")
def upsert_campaign(body: Dict[str, Any]):
    cid = body.pop("id", None) or None
    return {"id": save_campaign(body, cid)}


@app.delete("/api/campaigns/{cid}")
def remove_campaign(cid: str):
    return {"ok": delete_campaign(cid)}


# ----------------------------------------------------------------- live
class LiveIn(BaseModel):
    url: str
    campaign_id: Optional[str] = None
    preset: Optional[str] = None
    sensitivity: Optional[float] = 2.2
    quality: Optional[str] = "data_saver"


_live_clips_cache: List[Dict[str, Any]] = []
_live_clips_cache_time: float = 0.0
_live_clips_cache_lock = threading.Lock()


def _get_clip_creation_time(meta_path: str, meta_data: Optional[Dict[str, Any]] = None) -> float:
    """Return an immutable timestamp for a clip so rating/editing it never changes its display order."""
    if meta_data and meta_data.get("created_at"):
        try:
            return float(meta_data["created_at"])
        except (ValueError, TypeError):
            pass

    if meta_data is None and os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("created_at"):
                return float(d["created_at"])
        except Exception:
            pass

    # Check the mp4 file's modification time (mp4 is never written to on rating/feedback)
    mp4_path = meta_path.replace("_metadata.json", ".mp4")
    if os.path.isfile(mp4_path):
        try:
            return os.path.getmtime(mp4_path)
        except Exception:
            pass

    # Extract date from folder (e.g. 2026-11-19) and time from filename (e.g. 132758)
    try:
        parts = os.path.normpath(meta_path).split(os.sep)
        date_str = parts[-2] if len(parts) >= 2 else ""
        fn = parts[-1]
        m = re.search(r"(\d{2})(\d{2})(\d{2})", fn)
        if m and re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            time_str = f"{date_str} {m.group(1)}:{m.group(2)}:{m.group(3)}"
            dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            return dt.timestamp()
    except Exception:
        pass

    try:
        return os.path.getctime(meta_path)
    except Exception:
        return os.path.getmtime(meta_path)


@app.get("/api/live")
def live_status():
    sessions = []
    for k, v in _live_sessions.items():
        is_thread_alive = v["thread"].is_alive() if v.get("thread") else False
        is_paused = getattr(v.get("clipper"), "is_paused", False)
        sess_info = {
            "id": k,
            "url": v["url"],
            "started": v["started"],
            "running": is_thread_alive or not is_paused,
        }
        clipper_inst = v.get("clipper")
        if clipper_inst and hasattr(clipper_inst, "get_telemetry"):
            try:
                sess_info["telemetry"] = clipper_inst.get_telemetry()
            except Exception as e:
                sess_info["telemetry"] = {"status": "error", "activity": str(e), "recent_events": []}
        sessions.append(sess_info)

    global _live_clips_cache, _live_clips_cache_time
    now = time.time()
    with _live_clips_cache_lock:
        if now - _live_clips_cache_time < 3.0 and _live_clips_cache:
            clips = _live_clips_cache
        else:
            meta_files = glob.glob(os.path.join(ROOT, "clips", "live", "*", "*_metadata.json"))
            meta_files.sort(key=_get_clip_creation_time, reverse=True)
            loaded_clips = []
            for meta in meta_files:
                try:
                    with open(meta, "r", encoding="utf-8") as f:
                        c_data = json.load(f)
                        c_time = float(c_data.get("created_at") or _get_clip_creation_time(meta, c_data))
                        c_data["created_at"] = c_time
                        dt = datetime.datetime.fromtimestamp(c_time)
                        c_data["formatted_time"] = dt.strftime("%b %d · %I:%M %p")

                        # Parse streamer identity from metadata or filename
                        fn = c_data.get("filename") or os.path.basename(meta)
                        s_url = (c_data.get("stream_url") or "").lower()
                        if "stableronaldo" in fn.lower() or "stableronaldo" in s_url:
                            c_data["streamer_name"] = "stableronaldo"
                            c_data["streamer_platform"] = "twitch"
                        elif "jynxzi" in fn.lower() or "jynxzi" in s_url:
                            c_data["streamer_name"] = "jynxzi"
                            c_data["streamer_platform"] = "twitch"
                        elif "twitch" in fn.lower() or "twitch.tv" in s_url:
                            m_tw = re.search(r"twitch_([a-zA-Z0-9_\-]+?)_\d{6}", fn)
                            c_data["streamer_name"] = m_tw.group(1) if m_tw else "twitch"
                            c_data["streamer_platform"] = "twitch"
                        elif "yt_" in fn.lower() or "youtube" in fn.lower() or "youtube.com" in s_url:
                            m_yt = re.search(r"(?:yt|youtube)_([a-zA-Z0-9_\-]+?)_\d{6}", fn)
                            c_data["streamer_name"] = m_yt.group(1) if m_yt else "youtube"
                            c_data["streamer_platform"] = "youtube"
                        else:
                            c_data["streamer_name"] = "stream"
                            c_data["streamer_platform"] = "live"

                        loaded_clips.append(c_data)
                except Exception:
                    pass
            loaded_clips.sort(key=lambda c: (float(c.get("created_at") or 0.0), c.get("filename") or ""), reverse=True)
            _live_clips_cache = loaded_clips
            _live_clips_cache_time = now
            clips = loaded_clips

    cfg = load_config()
    saved_sens = cfg.get("signals", {}).get("live_sensitivity", 2.2)
    return {"sessions": sessions, "clips": clips, "sensitivity": saved_sens}


class LiveSensIn(BaseModel):
    sensitivity: float


@app.post("/api/live/sensitivity")
def live_set_sensitivity(body: LiveSensIn):
    val = float(body.sensitivity)
    save_user_override("signals", {"live_sensitivity": val})
    updated = 0
    for v in _live_sessions.values():
        c = v.get("clipper")
        if c and hasattr(c, "update_sensitivity"):
            c.update_sensitivity(val)
            updated += 1
    return {"ok": True, "sensitivity": val, "sessions_updated": updated}


@app.post("/api/live")
def live_start(body: LiveIn):
    from clipper.live import LiveStreamClipper
    camp = load_campaign(body.campaign_id) if body.campaign_id else None
    lc = LiveStreamClipper(
        stream_url=body.url.strip(),
        quality=body.quality or "data_saver",
        preset_name=body.preset or "classic_yellow",
        campaign=camp,
        sensitivity=float(body.sensitivity or 2.2),
    )
    t = threading.Thread(target=lc.run_loop, daemon=True, name=f"live-{lc.stream_id}")
    t.start()
    clean_sid = re.sub(r"[^a-zA-Z0-9_]", "", lc.stream_id)[:10]
    sid = f"{int(time.time())}_{clean_sid}"
    _live_sessions[sid] = {"url": body.url, "clipper": lc, "thread": t, "started": time.strftime("%H:%M")}
    _save_active_sessions()
    return {"id": sid}


@app.delete("/api/live/{sid}")
def live_stop(sid: str):
    s = _live_sessions.pop(sid, None)
    if s:
        s["clipper"].stop()
    _save_active_sessions()
    return {"ok": True}


@app.post("/api/live/{sid}/pause")
def live_pause(sid: str):
    s = _live_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    c = s.get("clipper")
    if c and hasattr(c, "pause"):
        c.pause("Paused by user")
    _save_active_sessions()
    return {"ok": True, "paused": True}


@app.post("/api/live/{sid}/resume")
def live_resume(sid: str):
    s = _live_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    c = s.get("clipper")
    if c and hasattr(c, "resume"):
        c.resume()
    if s.get("thread") is None or not s["thread"].is_alive():
        t = threading.Thread(target=c.run_loop, daemon=True, name=f"live-{c.stream_id}")
        t.start()
        s["thread"] = t
    _save_active_sessions()
    return {"ok": True, "paused": False}


@app.post("/api/live/{sid}/save")
def live_save_recording(sid: str):
    s = _live_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    c = s.get("clipper")
    if not c or not os.path.isfile(c.buffer_file):
        raise HTTPException(404, "Buffer recording file not found")
    rec_dir = os.path.join(ROOT, "clips", "recordings")
    os.makedirs(rec_dir, exist_ok=True)
    ts_now = time.strftime("%Y%m%d_%H%M%S")
    clean_id = re.sub(r"[^a-zA-Z0-9_]", "", c.stream_id)[:15]
    dest_name = f"recording_{clean_id}_{ts_now}.ts"
    dest_path = os.path.join(rec_dir, dest_name)
    shutil.copyfile(c.buffer_file, dest_path)
    rel_path = os.path.join("clips", "recordings", dest_name).replace("\\", "/")
    size_mb = round(os.path.getsize(dest_path) / (1024 * 1024), 1)
    return {"ok": True, "path": rel_path, "filename": dest_name, "size_mb": size_mb}


@app.delete("/api/live/clips/{filename}")
def live_delete_clip(filename: str, purge: bool = False):
    """Delete clip media file to reclaim storage while permanently preserving AI rating & taste data."""
    base = os.path.splitext(filename)[0]
    clean_fn = os.path.basename(filename)
    deleted = 0
    freed_bytes = 0
    meta = None
    meta_path = None

    matched_files = glob.glob(os.path.join(ROOT, "clips", "live", "*", f"{base}*"))
    for f in matched_files:
        if f.endswith("_metadata.json") or f.endswith(".json"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    meta = json.load(fp)
                meta_path = f
            except Exception:
                pass

    # 1. Permanently preserve feedback and learning metadata in ai_feedback.json
    feedback_file = os.path.join(ROOT, "data", "ai_feedback.json")
    os.makedirs(os.path.dirname(feedback_file), exist_ok=True)
    feedback_data = []
    if os.path.isfile(feedback_file):
        try:
            with open(feedback_file, "r", encoding="utf-8") as fp:
                feedback_data = json.load(fp)
        except Exception:
            feedback_data = []
    prev_entry = next((e for e in feedback_data if os.path.basename(e.get("filename", "")) == clean_fn), {})

    user_rating = (meta or {}).get("user_rating", 0) or prev_entry.get("rating", 0)
    user_tag = (meta or {}).get("user_tag") or prev_entry.get("tag") or ("space_saving_deletion" if user_rating >= 3 else "deleted_by_user")
    entry = {
        "filename": clean_fn,
        "rating": user_rating if user_rating > 0 else 1,
        "tag": user_tag,
        "notes": (meta or {}).get("user_notes") or prev_entry.get("notes", "Media removed to free disk storage"),
        "hook_title": (meta or {}).get("hook_title") or prev_entry.get("hook_title", ""),
        "transcript": (meta or {}).get("transcript") or prev_entry.get("transcript", ""),
        "duration": (meta or {}).get("duration") or prev_entry.get("duration", 0),
        "sub_scores": (meta or {}).get("sub_scores") or prev_entry.get("sub_scores"),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "media_deleted": True,
    }

    feedback_data = [e for e in feedback_data if os.path.basename(e.get("filename", "")) != clean_fn]
    feedback_data.insert(0, entry)
    with open(feedback_file, "w", encoding="utf-8") as fp:
        json.dump(feedback_data[:500], fp, indent=2)

    # 2. Delete heavy video/audio media files
    for f in matched_files:
        ext = os.path.splitext(f)[1].lower()
        if purge or ext in (".mp4", ".ts", ".mkv", ".mov", ".wav", ".aac"):
            try:
                if os.path.isfile(f):
                    sz = os.path.getsize(f)
                    os.remove(f)
                    deleted += 1
                    freed_bytes += sz
            except Exception:
                pass

    freed_mb = round(freed_bytes / (1024 * 1024), 1)

    # 3. If not purged, update lightweight metadata JSON to reflect media deletion
    if not purge and meta and meta_path and os.path.isfile(meta_path):
        try:
            if not meta.get("created_at"):
                meta["created_at"] = _get_clip_creation_time(meta_path, meta)
            meta["media_deleted"] = True
            meta["freed_mb"] = freed_mb
            meta["path"] = ""
            with open(meta_path, "w", encoding="utf-8") as fp:
                json.dump(meta, fp, indent=2)
        except Exception:
            pass

    # Update in-memory live clips cache
    global _live_clips_cache
    with _live_clips_cache_lock:
        if purge:
            _live_clips_cache = [c for c in _live_clips_cache if c.get("filename") != clean_fn and os.path.basename(c.get("path", "")) != clean_fn]
        else:
            for c in _live_clips_cache:
                if c.get("filename") == clean_fn or os.path.basename(c.get("path", "")) == clean_fn:
                    c["media_deleted"] = True
                    c["freed_mb"] = freed_mb
                    c["path"] = ""
                    break

    return {
        "ok": True,
        "deleted": deleted,
        "freed_mb": freed_mb,
        "feedback_preserved": True,
        "filename": clean_fn,
    }


class ClipFeedbackIn(BaseModel):
    filename: str
    rating: int  # 1 to 5
    tag: Optional[str] = None
    notes: Optional[str] = None


@app.post("/api/clips/feedback")
def clip_feedback(body: ClipFeedbackIn):
    """Save user rating and feedback tag for a clip to train the local AI judge."""
    clean_fn = os.path.basename(body.filename)
    base = os.path.splitext(clean_fn)[0]
    updated_meta = None

    # Targeted search across clips/live/ date directories first, then job dirs
    matched_files = glob.glob(os.path.join(ROOT, "clips", "live", "*", f"{base}*_metadata.json"))
    if not matched_files:
        matched_files = glob.glob(os.path.join(ROOT, "clips", "live", "*", f"{base}*.json"))
    if not matched_files:
        matched_files = glob.glob(os.path.join(ROOT, "clips", "job_*", f"{base}*.json"))
    if not matched_files:
        matched_files = glob.glob(os.path.join(ROOT, "clips", "**", f"*{base}*metadata.json"), recursive=True)
    if not matched_files:
        matched_files = glob.glob(os.path.join(ROOT, "clips", "**", f"{base}.json"), recursive=True)

    for f in matched_files:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
            if not meta.get("created_at"):
                meta["created_at"] = _get_clip_creation_time(f, meta)
            if body.rating > 0:
                meta["user_rating"] = body.rating
            if body.tag is not None:
                meta["user_tag"] = body.tag
            if body.notes:
                meta["user_notes"] = body.notes
            with open(f, "w", encoding="utf-8") as fp:
                json.dump(meta, fp, indent=2)
            updated_meta = meta
            break
        except Exception:
            pass

    # Update in-memory live clips cache immediately in-place
    with _live_clips_cache_lock:
        for c in _live_clips_cache:
            if c.get("filename") == clean_fn or os.path.basename(c.get("path", "")) == clean_fn:
                if body.rating > 0:
                    c["user_rating"] = body.rating
                if body.tag is not None:
                    c["user_tag"] = body.tag
                if body.notes:
                    c["user_notes"] = body.notes
                break

    feedback_file = os.path.join(ROOT, "data", "ai_feedback.json")
    os.makedirs(os.path.dirname(feedback_file), exist_ok=True)
    feedback_data = []
    if os.path.isfile(feedback_file):
        try:
            with open(feedback_file, "r", encoding="utf-8") as fp:
                feedback_data = json.load(fp)
        except Exception:
            feedback_data = []

    prev_entry = next((e for e in feedback_data if os.path.basename(e.get("filename", "")) == clean_fn), {})
    final_rating = body.rating if body.rating > 0 else ((updated_meta or {}).get("user_rating") or prev_entry.get("rating", 0))
    final_tag = body.tag if body.tag is not None else ((updated_meta or {}).get("user_tag") or prev_entry.get("tag", ""))

    entry = {
        "filename": clean_fn,
        "rating": final_rating,
        "tag": final_tag,
        "notes": body.notes or prev_entry.get("notes", ""),
        "hook_title": (updated_meta or {}).get("hook_title", prev_entry.get("hook_title", "")),
        "transcript": (updated_meta or {}).get("transcript", prev_entry.get("transcript", "")),
        "duration": (updated_meta or {}).get("duration", prev_entry.get("duration", 0)),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    feedback_data = [e for e in feedback_data if os.path.basename(e.get("filename", "")) != clean_fn]
    feedback_data.insert(0, entry)

    with open(feedback_file, "w", encoding="utf-8") as fp:
        json.dump(feedback_data[:300], fp, indent=2)

    return {"ok": True, "filename": clean_fn, "rating": final_rating, "tag": final_tag, "total_feedback_samples": len(feedback_data)}


@app.get("/api/clips/feedback")
def get_feedback_dataset():
    """Retrieve the training dataset of user ratings."""
    feedback_file = os.path.join(ROOT, "data", "ai_feedback.json")
    if not os.path.isfile(feedback_file):
        return []
    try:
        with open(feedback_file, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return []


# ----------------------------------------------------------------- publish (accounts + plan)
class SocialAccountIn(BaseModel):
    id: Optional[str] = None
    name: str
    max_clips_per_day: int = 4
    schedule_slots: List[str] = ["11:00", "15:00", "19:00", "22:00"]
    chrome_profile: str = ""
    platforms: Dict[str, Any] = {}


class OpenIn(BaseModel):
    account_id: str
    urls: List[str] = []
    login: bool = False        # open the sign-in pages of the account's platforms instead


class SchedulePostIn(BaseModel):
    account_id: str
    clip_path: str
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_time: Optional[str] = None
    mode: str = "plan"                       # "plan" (you post, using each site's own scheduler) or "auto" (API upload)
    meta: Optional[Dict[str, Any]] = None    # the clip's generated captions (youtube_title, reels_caption, ...)


class CredentialsIn(BaseModel):
    platform: str
    access_token: str
    page_id: Optional[str] = None       # Facebook Page
    ig_user_id: Optional[str] = None    # Instagram professional account


def _safe_clip_path(path: str) -> str:
    """Only clips inside the project's own folders may be published."""
    full = os.path.abspath(path if os.path.isabs(path) else os.path.join(ROOT, path))
    if not any(full.startswith(d + os.sep) for d in MEDIA_DIRS):
        raise HTTPException(400, "That file is outside the clips folders.")
    return full


@app.get("/api/social/accounts")
def get_social_accounts():
    out = []
    for aid, acc in load_accounts().items():
        item = dict(acc)
        item["id"] = aid
        item["today_count"] = get_daily_scheduled_count(aid)
        d, t = calculate_next_slot(aid)
        item["next_slot"] = f"{d}T{t}:00"
        item["connected"] = connection_state(aid)
        out.append(item)
    return out


@app.get("/api/social/chrome-profiles")
def chrome_profiles():
    return list_chrome_profiles()


@app.post("/api/social/open")
def open_sites(body: OpenIn):
    """Open the account's sites in its own Chrome profile (where it is already logged in)."""
    acc = get_account(body.account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    urls = body.urls
    if body.login:
        urls = [LOGIN_LINKS[p] for p, v in (acc.get("platforms") or {}).items() if v.get("enabled") and p in LOGIN_LINKS]
    try:
        return open_in_profile(acc.get("chrome_profile") or "", urls)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/social/info")
def social_info():
    return {"links": PLATFORM_LINKS, "platforms": list(PLATFORMS), "has_client_secret": has_client_secret()}


@app.post("/api/social/accounts")
def upsert_social_account(body: SocialAccountIn):
    data = (body.model_dump if hasattr(body, "model_dump") else body.dict)(exclude={"id"})
    saved_id = upsert_account(body.id, data)
    return {"id": saved_id, "account": get_account(saved_id)}


@app.delete("/api/social/accounts/{aid}")
def remove_social_account(aid: str):
    return {"ok": delete_account(aid)}


@app.post("/api/social/client-secret")
async def upload_client_secret(file: UploadFile = File(...)):
    try:
        save_client_secret(await file.read())
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/social/accounts/{aid}/connect/youtube")
def connect_youtube_account(aid: str):
    """Opens Google's sign-in page in the browser; the login is saved when the user finishes."""
    if not get_account(aid):
        raise HTTPException(404, "Account not found")
    if not has_client_secret():
        raise HTTPException(400, "Add the Google client file first.")

    def run():
        try:
            connect_youtube(aid)
        except Exception as e:  # noqa: BLE001
            print(f"[publish] YouTube sign-in failed: {e}")
    threading.Thread(target=run, daemon=True, name="yt-signin").start()
    return {"ok": True}


@app.post("/api/social/accounts/{aid}/credentials")
def save_account_credentials(aid: str, body: CredentialsIn):
    if not get_account(aid):
        raise HTTPException(404, "Account not found")
    if body.platform == "facebook" and body.page_id:
        save_credentials(aid, "facebook", {"access_token": body.access_token, "page_id": body.page_id})
    elif body.platform == "instagram" and body.ig_user_id:
        save_credentials(aid, "instagram", {"access_token": body.access_token, "ig_user_id": body.ig_user_id})
    elif body.platform == "tiktok":
        save_credentials(aid, "tiktok", {"access_token": body.access_token})
    else:
        raise HTTPException(400, "Missing the Page ID / Instagram user ID for that platform.")
    return {"ok": True, "connected": connection_state(aid)}


@app.delete("/api/social/accounts/{aid}/connect/{platform}")
def disconnect_account(aid: str, platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(400, "Unknown platform")
    ok = disconnect(aid, platform)
    return {"ok": ok, "connected": connection_state(aid)}


@app.get("/api/social/queue")
def get_social_queue():
    return queue_view()


@app.post("/api/social/schedule")
def schedule_post(body: SchedulePostIn):
    clip = _safe_clip_path(body.clip_path)
    try:
        return schedule_clip_to_account(
            account_id=body.account_id, clip_path=clip, title=body.title,
            description=body.description, target_time_iso=body.scheduled_time, mode=body.mode, meta=body.meta)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Could not add to the plan: {e}")


@app.post("/api/social/queue/{pid}/{platform}/{action}")
def update_platform(pid: str, platform: str, action: str):
    if action in ("posted", "unposted"):
        ok = mark_platform(pid, platform, action == "posted")
    elif action == "retry":
        ok = retry_platform(pid, platform)
    else:
        raise HTTPException(400, "Unknown action")
    if not ok:
        raise HTTPException(404, "Nothing to update")
    return {"ok": True}


@app.delete("/api/social/queue/{pid}")
def cancel_scheduled_post(pid: str):
    return {"ok": delete_schedule_entry(pid)}


@app.post("/api/social/reveal")
def reveal_clip(body: Dict[str, str]):
    """Open Explorer with the clip selected, so it can be picked in the platform's upload box."""
    full = _safe_clip_path(body.get("path", ""))
    if not os.path.isfile(full):
        raise HTTPException(404, "Clip file not found")
    if hasattr(os, "startfile"):
        subprocess.Popen(["explorer", "/select,", full])
    return {"ok": True, "folder": os.path.dirname(full)}


@app.post("/api/social/upload-file")
async def upload_custom_clip(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".mp4", ".mov", ".mkv", ".webm"):
        raise HTTPException(400, "Supported formats: .mp4, .mov, .mkv, .webm")
    upload_dir = os.path.join(ROOT, "clips", "custom_uploads")
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = os.path.basename(file.filename).replace(" ", "_")
    target_path = os.path.join(upload_dir, safe_name)
    base, fext = os.path.splitext(safe_name)
    counter = 1
    while os.path.exists(target_path):
        target_path = os.path.join(upload_dir, f"{base}_{counter}{fext}")
        counter += 1
    with open(target_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "filename": os.path.basename(target_path),
            "path": os.path.relpath(target_path, ROOT).replace("\\", "/"), "full_path": target_path}


@app.get("/api/social/library")
def clip_library():
    """Every clip that can be planned: rendered job clips plus files dropped in from Explorer."""
    taken = {os.path.normpath(os.path.abspath(e.get("clip_path", ""))).lower() for e in queue_view() if e["status"] != "FAILED"}
    out = []
    for j in sorted(load_jobs().values(), key=lambda x: x.get("created_at") or "", reverse=True):
        if j.get("is_dev_test"):
            continue
        for c in (j.get("clips") or []):
            p = c.get("path")
            if not p or c.get("status") == "REJECTED":
                continue
            full = os.path.abspath(p if os.path.isabs(p) else os.path.join(ROOT, p))
            if os.path.isfile(full):
                out.append({"path": p, "name": os.path.basename(p), "title": c.get("hook_title") or "", "job": j["id"],
                            "kept": c.get("status") == "KEPT", "meta": c.get("platforms") or {},
                            "used": os.path.normpath(full).lower() in taken})
    cu = os.path.join(ROOT, "clips", "custom_uploads")
    if os.path.isdir(cu):
        for n in sorted(os.listdir(cu)):
            if n.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                rel = os.path.join("clips", "custom_uploads", n).replace("\\", "/")
                full = os.path.join(cu, n)
                out.append({"path": rel, "name": n, "title": os.path.splitext(n)[0].replace("_", " "), "job": None, "kept": True,
                            "meta": {}, "used": os.path.normpath(full).lower() in taken})
    return out


# ----------------------------------------------------------------- bounties & rewards
class BountyIn(BaseModel):
    id: Optional[str] = None
    title: str
    streamer: Optional[str] = ""
    source: Optional[str] = "custom"
    source_name: Optional[str] = "Custom / Discord"
    payout_rate: Optional[str] = "$3.00 CPM"
    pool_size: Optional[str] = ""
    budget_remaining: Optional[str] = ""
    status: Optional[str] = "ACTIVE"
    platform: Optional[str] = "twitch"
    allowed_platforms: Optional[List[str]] = ["tiktok", "youtube", "instagram"]
    required_hashtags: Optional[List[str]] = []
    required_disclosure: Optional[str] = ""
    min_clip_length: Optional[int] = 15
    max_clip_length: Optional[int] = 60
    url: Optional[str] = ""
    rules_note: Optional[str] = ""
    assigned_account_id: Optional[str] = None


class LinkAccountIn(BaseModel):
    account_id: Optional[str] = None


@app.get("/api/bounties")
def get_bounties():
    return load_bounties()


@app.post("/api/bounties")
def create_or_update_bounty(body: BountyIn):
    data = (body.model_dump if hasattr(body, "model_dump") else body.dict)()
    bid = upsert_bounty(data)
    return {"id": bid, "bounty": get_bounty(bid)}


@app.delete("/api/bounties/{bid}")
def remove_bounty(bid: str):
    return {"ok": delete_bounty(bid)}


@app.post("/api/bounties/{bid}/import")
def import_bounty(bid: str, body: Optional[LinkAccountIn] = None):
    acc_id = body.account_id if body else None
    try:
        camp = import_bounty_as_campaign(bid, account_id=acc_id)
        return {"ok": True, "campaign": camp}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/bounties/{bid}/link-account")
def link_bounty(bid: str, body: LinkAccountIn):
    ok = link_bounty_account(bid, body.account_id)
    return {"ok": ok, "bounty": get_bounty(bid)}


def _start_ollama_in_background():
    """Studio opens -> Ollama is started too (hidden), so the AI judge can use its model."""
    llm = load_config().get("llm", {})
    if llm.get("autostart_ollama", True):
        threading.Thread(target=ensure_ollama_running, args=(llm.get("endpoint", "http://localhost:11434"),),
                         daemon=True, name="ollama-autostart").start()


if __name__ == "__main__":
    ensure_worker_running()
    _start_ollama_in_background()
    print("\n  CLIP AUTO Studio  ->  http://127.0.0.1:8765\n")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
