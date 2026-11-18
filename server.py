"""CLIP AUTO studio: local web UI backend (FastAPI).

Run:  .\\.venv\\Scripts\\python.exe server.py      then open http://127.0.0.1:8765

Wraps the existing pipeline/queue. Nothing here uploads anything anywhere; it only
serves files on your own machine to your own browser (bound to 127.0.0.1).
"""
import glob
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

from clipper.campaigns import delete_campaign, list_campaigns, load_campaign, save_campaign  # noqa: E402
from clipper.config import load_config, save_user_override  # noqa: E402
from clipper.judge import ensure_ollama_running, is_ollama_available  # noqa: E402
from clipper.pipeline import rerender_clip  # noqa: E402
from clipper.queue import (  # noqa: E402
    add_job, cancel_job, clear_finished_jobs, confirm_render_moments, delete_job,
    ensure_worker_running, load_jobs, mutate_job, pause_job, resume_job,
)
from clipper.social import (  # noqa: E402
    calculate_next_slot, delete_account, delete_schedule_entry, get_account,
    get_assigned_clip_account, get_daily_scheduled_count, load_accounts,
    load_schedule_queue, schedule_clip_to_account, upsert_account,
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


@app.on_event("startup")
def _on_startup():
    ensure_worker_running()
    _start_ollama_in_background()


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
        return f.read()


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
    return {
        "gpu": _gpu_status(),
        "ollama": {"running": ok, "models": models},
        "llm": {"enabled": bool(llm.get("enabled")), "model": llm.get("model")},
        "whisper_model": cfg.get("audio", {}).get("whisper_model", "large-v3"),
        "presets": presets,
        "polish": {"progress_bar": cfg.get("polish", {}).get("progress_bar", True)},
        "gameplay_aspect": cfg.get("facecam", {}).get("gameplay_aspect", "16:9"),
    }


class SettingsIn(BaseModel):
    llm_enabled: Optional[bool] = None
    llm_model: Optional[str] = None
    whisper_model: Optional[str] = None
    progress_bar: Optional[bool] = None
    gameplay_aspect: Optional[str] = None


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
        queue = load_schedule_queue()
        assigned_map = {}
        for p in queue:
            cp = p.get("clip_path")
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


@app.get("/api/live")
def live_status():
    sessions = [{"id": k, "url": v["url"], "started": v["started"],
                 "running": v["thread"].is_alive()} for k, v in _live_sessions.items()]
    clips = []
    for meta in sorted(glob.glob(os.path.join("clips", "live", "*", "*_metadata.json")), reverse=True)[:60]:
        try:
            with open(meta, "r", encoding="utf-8") as f:
                clips.append(json.load(f))
        except Exception:
            pass
    return {"sessions": sessions, "clips": clips}


@app.post("/api/live")
def live_start(body: LiveIn):
    from clipper.live import LiveStreamClipper
    camp = load_campaign(body.campaign_id) if body.campaign_id else None
    lc = LiveStreamClipper(stream_url=body.url.strip(), preset_name=body.preset or "classic_yellow",
                           campaign=camp)
    t = threading.Thread(target=lc.run_loop, daemon=True, name=f"live-{lc.stream_id}")
    t.start()
    sid = str(int(time.time()))
    _live_sessions[sid] = {"url": body.url, "clipper": lc, "thread": t, "started": time.strftime("%H:%M")}
    return {"id": sid}


@app.delete("/api/live/{sid}")
def live_stop(sid: str):
    s = _live_sessions.pop(sid, None)
    if s:
        s["clipper"].stop()
    return {"ok": True}


# ----------------------------------------------------------------- social & scheduling
class SocialAccountIn(BaseModel):
    id: Optional[str] = None
    name: str
    max_clips_per_day: int = 4
    schedule_slots: List[str] = ["11:00", "15:00", "19:00", "22:00"]
    platforms: Dict[str, Any] = {}


class SchedulePostIn(BaseModel):
    account_id: str
    clip_path: str
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_time: Optional[str] = None


@app.get("/api/social/accounts")
def get_social_accounts():
    accounts = load_accounts()
    out = []
    for aid, acc in accounts.items():
        today_count = get_daily_scheduled_count(aid)
        next_d, next_t = calculate_next_slot(aid)
        item = dict(acc)
        item["id"] = aid
        item["today_count"] = today_count
        item["next_slot"] = f"{next_d} {next_t}"
        out.append(item)
    return out


@app.post("/api/social/accounts")
def upsert_social_account(body: SocialAccountIn):
    aid = body.id or f"acc_{int(time.time())}"
    data = (body.model_dump if hasattr(body, "model_dump") else body.dict)(exclude={"id"})
    saved_id = upsert_account(aid, data)
    return {"id": saved_id, "account": get_account(saved_id)}


@app.delete("/api/social/accounts/{aid}")
def remove_social_account(aid: str):
    ok = delete_account(aid)
    return {"ok": ok}


@app.get("/api/social/queue")
def get_social_queue():
    q = load_schedule_queue()
    q.sort(key=lambda x: x.get("scheduled_time") or "", reverse=True)
    return q


@app.post("/api/social/schedule")
def schedule_post(body: SchedulePostIn):
    try:
        entry = schedule_clip_to_account(
            account_id=body.account_id,
            clip_path=body.clip_path,
            title=body.title,
            description=body.description,
            target_time_iso=body.scheduled_time,
        )
        return entry
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"Scheduling error: {e}")


@app.delete("/api/social/queue/{pid}")
def cancel_scheduled_post(pid: str):
    ok = delete_schedule_entry(pid)
    return {"ok": ok}


@app.post("/api/social/upload-file")
async def upload_custom_clip(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1].lower()
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

    rel_path = os.path.relpath(target_path, ROOT).replace("\\", "/")
    return {
        "ok": True,
        "filename": os.path.basename(target_path),
        "path": rel_path,
        "full_path": target_path,
    }


if __name__ == "__main__":
    ensure_worker_running()
    _start_ollama_in_background()
    print("\n  CLIP AUTO Studio  ->  http://127.0.0.1:8765\n")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
