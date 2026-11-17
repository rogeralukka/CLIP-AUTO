"""Background job queue with persistent state, real stage tracking, cancellation, pausing, and moment review."""
import json
import logging
import os
import shutil
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .campaigns import load_campaign, screen_transcript_for_risks
from .metadata import generate_clip_metadata
from .pipeline import detect_pipeline_moments, render_pipeline_moments, run_pipeline
from .process_manager import (
    JobCancelledException,
    JobPausedException,
    cancel_job_processes,
    check_cancelled_or_paused,
    is_job_cancelled,
    is_job_paused,
    log_job_message,
    pause_job_processes,
    register_job,
    resume_job_event,
)

logger = logging.getLogger("clipper.queue")

JOBS_FILE = "jobs.json"
_LOCK = threading.Lock()
_WORKER_THREAD: Optional[threading.Thread] = None


def _read_jobs_unlocked() -> Dict[str, Any]:
    if not os.path.isfile(JOBS_FILE):
        return {}
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_jobs_unlocked(jobs: Dict[str, Any]):
    temp_file = JOBS_FILE + f".tmp_{os.getpid()}_{threading.get_ident()}"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)
        for attempt in range(10):
            try:
                os.replace(temp_file, JOBS_FILE)
                break
            except (PermissionError, OSError):
                time.sleep(0.03 * (attempt + 1))
    except Exception as e:
        logger.warning(f"Error writing jobs file: {e}")
        try:
            if os.path.isfile(temp_file):
                os.remove(temp_file)
        except Exception:
            pass


def load_jobs() -> Dict[str, Any]:
    """Load job history and queue from disk under lock."""
    with _LOCK:
        return _read_jobs_unlocked()


def save_jobs(jobs: Dict[str, Any]):
    """Save job state to disk under lock."""
    with _LOCK:
        _write_jobs_unlocked(jobs)


def mutate_jobs(fn: Callable[[Dict[str, Any]], Any]) -> Any:
    """Safely mutate the jobs dictionary under lock and persist to disk."""
    with _LOCK:
        jobs = _read_jobs_unlocked()
        res = fn(jobs)
        _write_jobs_unlocked(jobs)
        return res


def mutate_job(job_id: str, fn: Callable[[Dict[str, Any]], None]):
    """Safely mutate a single job under lock and persist to disk."""
    with _LOCK:
        jobs = _read_jobs_unlocked()
        if job_id in jobs:
            fn(jobs[job_id])
            _write_jobs_unlocked(jobs)


def init_queue():
    """Inspect queue on startup: mark running jobs as INTERRUPTED; paused jobs stay PAUSED."""
    def _init(jobs):
        for jid, job in jobs.items():
            if job.get("status") == "RUNNING":
                job["status"] = "INTERRUPTED"
                job["status_message"] = "Interrupted by application restart. Click Resume or Delete."
                job["stage_details"] = "Interrupted."
            # Note: jobs with status == "PAUSED" deliberately remain PAUSED! (Requirement 1)
    mutate_jobs(_init)


init_queue()


def check_duplicate_job(vod_input: str, campaign_id: Optional[str]) -> Optional[str]:
    """Check if an active job already exists with identical VOD input and campaign."""
    clean_vod = vod_input.strip().lower()
    clean_camp = (campaign_id or "").strip().lower()
    jobs = load_jobs()
    for jid, j in jobs.items():
        if j.get("status") in ("QUEUED", "RUNNING", "PAUSED", "AWAITING_SELECTION"):
            j_vod = (j.get("vod_input") or "").strip().lower()
            j_camp = (j.get("campaign_id") or "").strip().lower()
            if j_vod == clean_vod and j_camp == clean_camp:
                return jid
    return None


def add_job(
    vod_input: str,
    campaign_id: Optional[str] = None,
    chat_input: Optional[str] = None,
    quality: str = "1080p",
    mode: str = "AUTO",
    top: Optional[int] = None,
    safety_max: int = 50,
    z_thresh: float = 2.0,
    merge_gap: int = 15,
    preset: Optional[str] = None,
    use_cpu: bool = False,
    auto_render: bool = False,
    is_dev_test: bool = False,
) -> str:
    """Add a new clipping job to the queue with quality choice."""
    job_id = str(uuid.uuid4())[:8]
    register_job(job_id)

    campaign_data = None
    if campaign_id and campaign_id != "none":
        try:
            campaign_data = load_campaign(campaign_id)
        except Exception as e:
            logger.warning(f"Could not load campaign {campaign_id}: {e}")

    effective_preset = preset or (campaign_data.get("preset") if campaign_data else "classic_yellow")

    new_job = {
        "id": job_id,
        "is_dev_test": is_dev_test,
        "vod_input": vod_input,
        "chat_input": chat_input,
        "quality": quality,
        "campaign_id": campaign_id if campaign_id != "none" else None,
        "campaign": campaign_data,
        "mode": mode,
        "top": top if mode == "MANUAL" else None,
        "safety_max": safety_max,
        "z_thresh": z_thresh,
        "merge_gap": merge_gap,
        "preset": effective_preset,
        "use_cpu": use_cpu,
        "auto_render": auto_render,
        "status": "QUEUED",
        "progress": 0,
        "status_message": "Waiting in queue...",
        "current_stage": "download",
        "stages": {
            "download": "WAITING",
            "detect": "WAITING",
            "selection": "WAITING",
            "transcribe": "WAITING",
            "render": "WAITING",
        },
        "stage_details": "Waiting in queue...",
        "heartbeat": time.time(),
        "start_time": None,
        "eta_seconds": None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at": None,
        "detected_moments": [],
        "selected_indices": None,
        "video_path": None,
        "clips": [],
        "error": None,
    }

    log_job_message(job_id, f"Job #{job_id} created for {vod_input} (quality={quality}, mode={mode}).")

    def _add(jobs):
        jobs[job_id] = new_job

    mutate_jobs(_add)
    ensure_worker_running()
    return job_id


def pause_job(job_id: str):
    """Pause a running or queued job safely, keeping all partial files (Requirement 1)."""
    logger.info(f"Pausing job {job_id}...")
    log_job_message(job_id, "Pause signal sent to job.")
    pause_job_processes(job_id)

    def _pause(job):
        job["status"] = "PAUSED"
        job["status_message"] = "Job paused. Click Resume to continue."
        job["stage_details"] = "Paused. Hardware resources freed."

    mutate_job(job_id, _pause)


def resume_job(job_id: str):
    """Resume a paused or interrupted job from where it left off (Requirement 1)."""
    logger.info(f"Resuming job {job_id}...")
    register_job(job_id)
    resume_job_event(job_id)
    log_job_message(job_id, "Resuming job execution...")

    def _resume(job):
        if job.get("detected_moments") and job.get("selected_indices") is None and not job.get("auto_render"):
            job["status"] = "AWAITING_SELECTION"
            job["stage_details"] = "Ready for moment review."
        else:
            job["status"] = "QUEUED"
            job["stage_details"] = "Queued to resume."
        job["status_message"] = "Resuming..."

    mutate_job(job_id, _resume)
    ensure_worker_running()


def cancel_job(job_id: str):
    """Cancel a queued, running, or paused job, terminate child processes, and clean up temp files (Requirement 1)."""
    logger.info(f"Cancelling job {job_id}...")
    log_job_message(job_id, "Cancel signal sent to job.")
    cancel_job_processes(job_id)

    def _cancel(job):
        job["status"] = "CANCELLED"
        job["status_message"] = "Job cancelled by user."
        job["stage_details"] = "Cancelled."
        job["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    mutate_job(job_id, _cancel)

    # Delete half-finished temp directory, but keep downloads cache
    temp_dir = os.path.join("clips", f"job_{job_id}")
    if os.path.isdir(temp_dir):
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info(f"Cleaned up partial temp folder: {temp_dir}")
        except Exception as e:
            logger.warning(f"Could not remove partial dir {temp_dir}: {e}")


def cancel_all():
    """Cancel all active (queued, running, paused, or awaiting selection) jobs."""
    jobs = load_jobs()
    for jid, j in jobs.items():
        if j.get("status") in ("QUEUED", "RUNNING", "PAUSED", "AWAITING_SELECTION"):
            cancel_job(jid)


def clear_finished_jobs():
    """Delete completed, cancelled, and failed jobs from the jobs list."""
    def _clear(jobs):
        to_delete = [
            jid for jid, j in jobs.items()
            if j.get("status") in ("COMPLETED", "CANCELLED", "FAILED")
        ]
        for jid in to_delete:
            del jobs[jid]

    mutate_jobs(_clear)


def delete_job(job_id: str):
    """Permanently delete a job and its generated clips folder."""
    cancel_job_processes(job_id)

    def _delete(jobs):
        if job_id in jobs:
            del jobs[job_id]

    mutate_jobs(_delete)
    temp_dir = os.path.join("clips", f"job_{job_id}")
    if os.path.isdir(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


def confirm_render_moments(
    job_id: str,
    selected_indices: List[int],
    updated_moments: Optional[List[Dict[str, Any]]] = None,
):
    """Set selected moment indices, save any updated hook titles/metadata, and transition job to QUEUED for rendering."""
    register_job(job_id)
    resume_job_event(job_id)

    def _confirm(job):
        if updated_moments is not None:
            job["detected_moments"] = updated_moments
        job["selected_indices"] = selected_indices
        job["stages"]["selection"] = "DONE"
        job["stages"]["transcribe"] = "WAITING"
        job["stages"]["render"] = "WAITING"
        job["status"] = "QUEUED"
        job["stage_details"] = f"Approved {len(selected_indices)} moments. Queued for rendering..."

    mutate_job(job_id, _confirm)
    ensure_worker_running()


def update_job_progress(
    job_id: str,
    stage: str,
    progress: int,
    message: str,
    meta: Optional[Dict[str, Any]] = None,
):
    """Update job stage, progress percentage, ETA, and heartbeat timestamp."""
    now = time.time()

    def _update(job):
        job["current_stage"] = stage
        job["progress"] = min(100, max(job.get("progress", 0), progress))
        job["stage_details"] = message
        job["status_message"] = message
        job["heartbeat"] = now

        stages = job.get("stages", {})
        if stage == "download":
            stages["download"] = "RUNNING"
        elif stage == "detect":
            stages["download"] = "DONE"
            stages["detect"] = "RUNNING"
        elif stage == "selection":
            stages["detect"] = "DONE"
            stages["selection"] = "RUNNING"
        elif stage in ("transcribe", "render"):
            stages["detect"] = "DONE"
            stages["selection"] = "DONE"
            if stage == "transcribe":
                stages["transcribe"] = "RUNNING"
            else:
                stages["transcribe"] = "DONE"
                stages["render"] = "RUNNING"
        job["stages"] = stages

        start_time = job.get("start_time")
        if start_time and job["progress"] > 5 and job["progress"] < 100:
            elapsed = now - start_time
            est_total = elapsed / (job["progress"] / 100.0)
            job["eta_seconds"] = max(0, int(est_total - elapsed))

    mutate_job(job_id, _update)


def _worker_loop():
    """Background worker executing queued jobs one by one with comprehensive stage tracking."""
    logger.info("Background clipping worker started.")
    while True:
        try:
            next_job_id = None
            with _LOCK:
                jobs = _read_jobs_unlocked()
                for jid, job in jobs.items():
                    # Pick only QUEUED jobs (PAUSED jobs are skipped)
                    if job.get("status") == "QUEUED":
                        next_job_id = jid
                        break

            if not next_job_id:
                time.sleep(1.0)
                continue

            register_job(next_job_id)

            if is_job_cancelled(next_job_id):
                continue
            if is_job_paused(next_job_id):
                continue

            # Mark job RUNNING
            def _set_running(j):
                j["start_time"] = j.get("start_time") or time.time()
                j["status"] = "RUNNING"
                j["heartbeat"] = time.time()

            mutate_job(next_job_id, _set_running)

            current_job_data = load_jobs().get(next_job_id)
            if not current_job_data:
                continue

            output_folder = os.path.join("clips", f"job_{next_job_id}")
            os.makedirs(output_folder, exist_ok=True)

            def job_progress_cb(stage: str, pct: int, msg: str, meta: Dict[str, Any]):
                update_job_progress(next_job_id, stage, pct, msg, meta)

            # PHASE 1: Moment Detection
            video_path = current_job_data.get("video_path")
            detected_moments = current_job_data.get("detected_moments") or []

            if not detected_moments or not video_path or not (os.path.isfile(video_path) and os.path.getsize(video_path) > 1024):
                update_job_progress(next_job_id, "download", 5, "Resolving & downloading stream source...")

                top_limit = current_job_data.get("top") if current_job_data.get("mode") == "MANUAL" else None
                quality_choice = current_job_data.get("quality", "1080p")

                v_path, c_path, moments = detect_pipeline_moments(
                    vod_input=current_job_data["vod_input"],
                    chat_input=current_job_data.get("chat_input"),
                    quality=quality_choice,
                    top=top_limit,
                    z_thresh=current_job_data.get("z_thresh", 2.0),
                    safety_max=current_job_data.get("safety_max", 50),
                    merge_gap=current_job_data.get("merge_gap", 15),
                    cache_dir="downloads",
                    job_id=next_job_id,
                    progress_callback=job_progress_cb,
                )

                check_cancelled_or_paused(next_job_id)

                video_path = v_path
                detected_moments = moments

                if not moments:
                    def _no_moments(j):
                        j["status"] = "COMPLETED"
                        j["progress"] = 100
                        j["status_message"] = "No hype moments detected above threshold."
                        j["stage_details"] = "Completed (0 moments)."
                        j["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    mutate_job(next_job_id, _no_moments)
                    continue

                # Check if review requested
                if not current_job_data.get("auto_render", False) and current_job_data.get("selected_indices") is None:
                    def _pause_sel(j):
                        j["video_path"] = v_path
                        j["detected_moments"] = moments
                        j["status"] = "AWAITING_SELECTION"
                        j["progress"] = 35
                        j["stages"]["detect"] = "DONE"
                        j["stages"]["selection"] = "RUNNING"
                        j["status_message"] = f"Found {len(moments)} hype moments. Review & select moments."
                        j["stage_details"] = f"Awaiting review of {len(moments)} moments."
                    mutate_job(next_job_id, _pause_sel)
                    logger.info(f"Job {next_job_id} is AWAITING_SELECTION ({len(moments)} candidate moments).")
                    continue
                else:
                    def _save_moments(j):
                        j["video_path"] = v_path
                        j["detected_moments"] = moments
                    mutate_job(next_job_id, _save_moments)

            # PHASE 2: Rendering Selected Moments
            check_cancelled_or_paused(next_job_id)
            current_job_data = load_jobs().get(next_job_id, {})
            selected_indices = current_job_data.get("selected_indices")
            if selected_indices is not None:
                moments_to_render = [
                    m for idx, m in enumerate(detected_moments)
                    if idx in selected_indices
                ]
            else:
                moments_to_render = detected_moments

            if not moments_to_render:
                def _zero_sel(j):
                    j["status"] = "COMPLETED"
                    j["progress"] = 100
                    j["status_message"] = "0 moments were selected to render."
                    j["stage_details"] = "Completed (0 moments)."
                    j["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                mutate_job(next_job_id, _zero_sel)
                continue

            update_job_progress(
                next_job_id,
                "render",
                35,
                f"Rendering {len(moments_to_render)} clips with caption preset '{current_job_data.get('preset')}'..."
            )

            campaign = current_job_data.get("campaign") or {}
            overlay_config = campaign.get("overlay")

            raw_clips = render_pipeline_moments(
                video_path=video_path,
                moments=moments_to_render,
                output_dir=output_folder,
                preset_name=current_job_data.get("preset", "classic_yellow"),
                cpu=current_job_data.get("use_cpu", False),
                overlay=overlay_config,
                job_id=next_job_id,
                progress_callback=job_progress_cb,
            )

            check_cancelled_or_paused(next_job_id)

            # PHASE 3: Screening Risks & Metadata
            update_job_progress(next_job_id, "render", 95, "Screening risk words & generating clip metadata...")

            processed_clips = []
            risk_words = campaign.get("risk_words")

            for c in raw_clips:
                transcript = c.get("transcript", "")
                is_flagged, matched_risks = screen_transcript_for_risks(
                    transcript, custom_risk_words=risk_words
                )
                meta = generate_clip_metadata(c, campaign=campaign)

                c_info = dict(c)
                c_info["is_flagged"] = is_flagged
                c_info["flagged_words"] = matched_risks
                c_info["suggested_title"] = meta["title"]
                c_info["suggested_description"] = meta["description"]
                c_info["suggested_hashtags"] = meta["hashtags"]
                c_info["status"] = "PENDING"
                processed_clips.append(c_info)

            # Finalize Job
            def _finish(j):
                if j.get("status") not in ("CANCELLED", "PAUSED"):
                    j["status"] = "COMPLETED"
                    j["progress"] = 100
                    j["stages"]["render"] = "DONE"
                    j["stages"]["transcribe"] = "DONE"
                    j["status_message"] = f"Finished! {len(processed_clips)} clips ready for review."
                    j["stage_details"] = "Completed successfully."
                    j["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    j["clips"] = processed_clips

            mutate_job(next_job_id, _finish)
            logger.info(f"Job {next_job_id} successfully completed ({len(processed_clips)} clips).")

        except JobPausedException:
            logger.info(f"Job {next_job_id} was paused during execution.")
            def _pause_exc(j):
                j["status"] = "PAUSED"
                j["status_message"] = "Job paused by user. Click Resume to continue."
                j["stage_details"] = "Paused. Partial files and clips preserved."
            mutate_job(next_job_id, _pause_exc)

        except JobCancelledException:
            logger.info(f"Job {next_job_id} was cancelled during execution.")
            def _cancel_exc(j):
                j["status"] = "CANCELLED"
                j["status_message"] = "Job cancelled by user."
                j["stage_details"] = "Cancelled."
                j["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            mutate_job(next_job_id, _cancel_exc)
            temp_dir = os.path.join("clips", f"job_{next_job_id}")
            if os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

        except Exception as e:
            logger.exception(f"Job {next_job_id} failed: {e}")
            def _fail(j):
                if j.get("status") not in ("CANCELLED", "PAUSED"):
                    j["status"] = "FAILED"
                    j["status_message"] = f"Failed: {e}"
                    j["stage_details"] = f"Error: {e}"
                    j["error"] = str(e)
                    j["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            mutate_job(next_job_id, _fail)

        time.sleep(1.0)


def ensure_worker_running():
    """Start background worker thread if not already active."""
    global _WORKER_THREAD
    with _LOCK:
        if _WORKER_THREAD is None or not _WORKER_THREAD.is_alive():
            _WORKER_THREAD = threading.Thread(target=_worker_loop, daemon=True, name="ClipperWorkerThread")
            _WORKER_THREAD.start()
