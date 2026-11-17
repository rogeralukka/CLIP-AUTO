"""Test suite for Pause/Resume engine, quality inspection with file sizes, and isolated per-job logs."""
import glob
import os
import subprocess
import sys
import time

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from clipper.downloader import get_available_qualities
from clipper.process_manager import get_job_logs
from clipper.queue import (
    add_job,
    cancel_job,
    load_jobs,
    pause_job,
    resume_job,
    save_jobs,
)
from tests.generate_test_assets import generate_synthetic_assets


def get_running_process_names():
    """List running processes matching ffmpeg, yt-dlp, or TwitchDownloader."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-Process -Name ffmpeg,yt-dlp,TwitchDownloaderCLI -ErrorAction SilentlyContinue | Select-Object -ExpandProperty ProcessName"],
        capture_output=True,
        text=True,
    )
    return [p.strip() for p in out.stdout.splitlines() if p.strip()]


def run_tests():
    print("=" * 70)
    print("  TEST 1: VIDEO QUALITY DROPDOWN & ESTIMATED SIZES")
    print("=" * 70)
    test_url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    print(f"Querying qualities for real video URL: {test_url}")
    qualities = get_available_qualities(test_url)

    print("\nAvailable Qualities Detected:")
    for q in qualities:
        print(f"  - {q['id']:10s} : {q['label']} (size: {q.get('size_mb', 0):.1f} MB, height: {q.get('height')})")

    q_ids = [q["id"] for q in qualities]
    assert "1080p" in q_ids, "1080p must be in available qualities"
    assert "best" in q_ids, "best must be in available qualities"
    assert "720p" in q_ids, "720p must be in available qualities"
    assert "480p" in q_ids, "480p must be in available qualities"

    has_sizes = any(q.get("size_mb", 0) > 0 for q in qualities)
    assert has_sizes, "Qualities must contain estimated sizes"
    print(">>> TEST 1 PASSED: All video quality options with estimated sizes detected successfully.")

    print("\n" + "=" * 70)
    print("  TEST 2: PAUSE & RESUME ON DOWNLOAD / EXECUTION")
    print("=" * 70)
    test_video, test_chat = generate_synthetic_assets(output_dir="tests/test_data")

    # Clear queue
    save_jobs({})

    job_id = add_job(
        vod_input=test_video,
        chat_input=test_chat,
        quality="1080p",
        campaign_id="sample_game_launch",
        mode="MANUAL",
        top=2,
        preset="classic_yellow",
        auto_render=True,
    )
    print(f"Queued Job #{job_id}")

    # Wait for job to start running
    for _ in range(15):
        jobs = load_jobs()
        if jobs.get(job_id, {}).get("status") == "RUNNING":
            print(f"Job #{job_id} is now RUNNING (stage: {jobs[job_id].get('current_stage')})")
            break
        time.sleep(0.5)

    # Pause Job
    print(f"\nPausing running Job #{job_id}...")
    pause_job(job_id)
    time.sleep(1.5)

    jobs = load_jobs()
    st_val = jobs.get(job_id, {}).get("status")
    print(f"Job #{job_id} status after pause: {st_val}")
    assert st_val == "PAUSED", f"Expected PAUSED, got {st_val}"

    # Check processes terminated (wait up to 3s for OS release)
    procs = []
    for _ in range(6):
        procs = get_running_process_names()
        if not procs:
            break
        time.sleep(0.5)

    print(f"Active processes during PAUSE: {procs or 'None (All subprocesses suspended/freed!)'}")
    assert len(procs) == 0, f"No subprocesses should be active during PAUSE: {procs}"

    # Resume Job
    print(f"\nResuming paused Job #{job_id}...")
    resume_job(job_id)
    time.sleep(1.0)

    jobs = load_jobs()
    st_val = jobs.get(job_id, {}).get("status")
    print(f"Job #{job_id} status after resume call: {st_val}")
    assert st_val in ("QUEUED", "RUNNING"), f"Expected QUEUED or RUNNING after resume, got {st_val}"

    # Wait for job to finish
    print(f"Waiting for Job #{job_id} to complete execution...")
    for _ in range(60):
        jobs = load_jobs()
        st_val = jobs.get(job_id, {}).get("status")
        if st_val in ("COMPLETED", "FAILED", "CANCELLED"):
            print(f"Job #{job_id} reached final status: {st_val} ({jobs[job_id].get('status_message')})")
            break
        prog = jobs.get(job_id, {}).get("progress", 0)
        details = jobs.get(job_id, {}).get("stage_details", "")
        print(f"  [Progress] {prog}% - {details}")
        time.sleep(1.5)

    jobs = load_jobs()
    assert jobs.get(job_id, {}).get("status") == "COMPLETED", "Job should complete after resume"
    print(">>> TEST 2 PASSED: Job successfully paused and resumed to completion.")

    print("\n" + "=" * 70)
    print("  TEST 3: CANCEL ON A PAUSED JOB")
    print("=" * 70)
    job_p = add_job(
        vod_input=test_video,
        chat_input=test_chat,
        quality="720p",
        campaign_id="sample_game_launch",
        mode="MANUAL",
        top=1,
        auto_render=True,
    )
    print(f"Queued Job #{job_p} for pause-cancel test...")
    pause_job(job_p)
    time.sleep(0.5)

    jobs = load_jobs()
    assert jobs.get(job_p, {}).get("status") == "PAUSED"
    print(f"Job #{job_p} is PAUSED. Now calling cancel_job...")

    cancel_job(job_p)
    time.sleep(0.5)

    jobs = load_jobs()
    st_cancel = jobs.get(job_p, {}).get("status")
    print(f"Job #{job_p} status after cancel: {st_cancel}")
    assert st_cancel == "CANCELLED", f"Expected CANCELLED, got {st_cancel}"
    print(">>> TEST 3 PASSED: Cancel works cleanly on PAUSED jobs.")

    print("\n" + "=" * 70)
    print("  TEST 4: PER-JOB ISOLATED LOG VERIFICATION")
    print("=" * 70)
    job_logs = get_job_logs(job_id, max_lines=30)
    print(f"Retrieved {len(job_logs)} isolated log lines for Job #{job_id}:")
    for line in job_logs[:5]:
        print(f"  | {line.strip()}")
    print("  ...")
    for line in job_logs[-3:]:
        print(f"  | {line.strip()}")

    assert len(job_logs) > 0, "Job must have isolated log lines"
    assert not any("synthetic_vod_other" in l for l in job_logs), "Should not contain lines from other jobs"
    print(">>> TEST 4 PASSED: Per-job logs are clean, isolated, and accessible.")

    print("\n" + "=" * 70)
    print("  ALL TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
