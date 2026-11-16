"""Verification script for queueing 3 jobs, cancelling 1 running and 1 queued job,
and verifying no stuck processes remain.
"""
import os
import subprocess
import sys
import time

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from clipper.queue import (
    add_job,
    cancel_job,
    ensure_worker_running,
    load_jobs,
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


def run_verification():
    print("=" * 65)
    print("  VERIFYING QUEUE SAFETY & CANCELLATION ENGINE")
    print("=" * 65)

    test_video, test_chat = generate_synthetic_assets(output_dir="tests/test_data")
    print(f"Using synthetic assets:\n  {test_video}\n  {test_chat}")

    # Ensure clean state for test
    save_jobs({})

    print("\n1. Queueing 3 jobs...")
    job1 = add_job(
        vod_input=test_video,
        chat_input=test_chat,
        campaign_id="sample_game_launch",
        mode="MANUAL",
        top=2,
        preset="classic_yellow",
        auto_render=True,
    )
    print(f"  - Queued Job 1: #{job1}")

    job2 = add_job(
        vod_input=test_video,
        chat_input=test_chat,
        campaign_id="sample_game_launch",
        mode="MANUAL",
        top=2,
        preset="neon_green",
        auto_render=True,
    )
    print(f"  - Queued Job 2: #{job2}")

    job3 = add_job(
        vod_input=test_video,
        chat_input=test_chat,
        campaign_id="sample_game_launch",
        mode="MANUAL",
        top=1,
        preset="clean_white",
        auto_render=True,
    )
    print(f"  - Queued Job 3: #{job3}")

    # Wait for Job 1 to transition to RUNNING
    print("\n2. Waiting for Job 1 to start running...")
    for _ in range(20):
        jobs = load_jobs()
        st1 = jobs.get(job1, {}).get("status")
        if st1 == "RUNNING":
            print(f"  - Job 1 is now RUNNING (stage: {jobs[job1].get('current_stage')}, progress: {jobs[job1].get('progress')}%)")
            break
        time.sleep(0.5)

    # Cancel Job 1 (running)
    print(f"\n3. Cancelling RUNNING Job 1 (#{job1})...")
    cancel_job(job1)
    time.sleep(1)

    jobs = load_jobs()
    st1 = jobs.get(job1, {}).get("status")
    print(f"  - Job 1 status after cancel: {st1}")
    assert st1 == "CANCELLED", f"Expected CANCELLED but got {st1}"

    # Verify Job 1 temp files were deleted
    job1_temp = os.path.join("clips", f"job_{job1}")
    print(f"  - Job 1 temp folder exists: {os.path.isdir(job1_temp)} (Expected False)")
    assert not os.path.isdir(job1_temp), f"Temp dir {job1_temp} should be deleted upon cancel"

    # Cancel Job 2 (queued)
    print(f"\n4. Cancelling QUEUED Job 2 (#{job2})...")
    cancel_job(job2)
    time.sleep(1)

    jobs = load_jobs()
    st2 = jobs.get(job2, {}).get("status")
    print(f"  - Job 2 status after cancel: {st2}")
    assert st2 == "CANCELLED", f"Expected CANCELLED but got {st2}"

    # Wait for Job 3 to run and complete
    print(f"\n5. Waiting for Job 3 (#{job3}) to complete...")
    for _ in range(60):
        jobs = load_jobs()
        st3 = jobs.get(job3, {}).get("status")
        if st3 in ("COMPLETED", "FAILED", "CANCELLED"):
            print(f"  - Job 3 finished with status: {st3} ({jobs[job3].get('status_message')})")
            break
        prog = jobs.get(job3, {}).get("progress", 0)
        stage = jobs.get(job3, {}).get("stage_details", "")
        print(f"    [Job 3] {prog}% - {stage}")
        time.sleep(1.5)

    jobs = load_jobs()
    st3 = jobs.get(job3, {}).get("status")
    assert st3 == "COMPLETED", f"Expected Job 3 to complete, got {st3}"

    # Check for any lingering ffmpeg, yt-dlp, or TwitchDownloaderCLI processes
    print("\n6. Checking for stuck or orphan processes in OS...")
    lingering = get_running_process_names()
    print(f"  - Lingering clipping processes: {lingering or 'None (Clean!)'}")
    assert len(lingering) == 0, f"Found stuck processes: {lingering}"

    print("\n" + "=" * 65)
    print("  ALL VERIFICATION CHECKS PASSED SUCCESSFULLY!")
    print("=" * 65)


if __name__ == "__main__":
    run_verification()
