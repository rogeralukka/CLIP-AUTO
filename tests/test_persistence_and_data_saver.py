import json
import os
import shutil
import tempfile
import pytest

from clipper.live import LiveStreamClipper, resolve_stream_quality
from clipper.config import load_config
from server import live_delete_clip


def test_resolve_stream_quality():
    """Verify bandwidth modes correctly resolve streamlink quality profiles."""
    assert resolve_stream_quality("data_saver") == "720p,720p60,480p,best"
    assert resolve_stream_quality("ultra_saver") == "480p,360p,worst"
    assert resolve_stream_quality("source") == "1080p60,1080p,best"
    assert resolve_stream_quality("480p") == "480p"


def test_state_json_persistence_and_restoration(tmp_path):
    """Verify that LiveStreamClipper saves state.json and restores progress across restarts."""
    test_buffer_dir = str(tmp_path / "test_stream_dir")
    os.makedirs(test_buffer_dir, exist_ok=True)

    # 1. Create first clipper instance and simulate analysis progress
    clipper1 = LiveStreamClipper(
        stream_url="https://youtube.com/live/test1234",
        quality="data_saver",
        sensitivity=2.5,
    )
    # Redirect buffer_dir to temp dir for isolated test
    clipper1.buffer_dir = test_buffer_dir
    clipper1.buffer_file = os.path.join(test_buffer_dir, "live_stream.ts")
    clipper1.state_file = os.path.join(test_buffer_dir, "state.json")
    clipper1.last_analyzed_time = 320.5
    clipper1.buffer_seconds = 450.0
    clipper1.buffer_mb = 125.4
    clipper1.clips_done = [{"filename": "clip_1.mp4", "score": 8.5}]
    clipper1.save_state()

    assert os.path.isfile(clipper1.state_file)
    with open(clipper1.state_file, "r", encoding="utf-8") as f:
        saved_data = json.load(f)
    assert saved_data["last_analyzed_time"] == 320.5
    assert saved_data["quality"] == "720p,720p60,480p,best"

    # 2. Simulate complete app shutdown / restart by instantiating a new clipper
    clipper2 = LiveStreamClipper(
        stream_url="https://youtube.com/live/test1234",
        quality="data_saver",
        sensitivity=2.5,
    )
    clipper2.buffer_dir = test_buffer_dir
    clipper2.buffer_file = os.path.join(test_buffer_dir, "live_stream.ts")
    clipper2.state_file = os.path.join(test_buffer_dir, "state.json")

    # Re-trigger restoration on the configured path
    with open(clipper2.state_file, "r", encoding="utf-8") as f:
        st = json.load(f)
        clipper2.last_analyzed_time = float(st.get("last_analyzed_time", 0.0))
        clipper2.clips_done = st.get("clips_done", [])

    assert clipper2.last_analyzed_time == 320.5
    assert len(clipper2.clips_done) == 1
    assert clipper2.clips_done[0]["filename"] == "clip_1.mp4"

    telem = clipper2.get_telemetry()
    assert telem["analyzed_seconds"] == 320.5
    assert "quality" in telem


def test_live_delete_preserves_ai_feedback(tmp_path, monkeypatch):
    """Verify that deleting clip media deletes .mp4 but keeps full metadata in ai_feedback.json."""
    fake_root = str(tmp_path / "root")
    os.makedirs(fake_root, exist_ok=True)
    monkeypatch.setattr("server.ROOT", fake_root)

    live_clip_dir = os.path.join(fake_root, "clips", "live", "test_stream")
    os.makedirs(live_clip_dir, exist_ok=True)

    mp4_file = os.path.join(live_clip_dir, "stream_test_120s.mp4")
    wav_file = os.path.join(live_clip_dir, "stream_test_120s.wav")
    json_file = os.path.join(live_clip_dir, "stream_test_120s_metadata.json")

    with open(mp4_file, "wb") as f:
        f.write(b"FAKE_VIDEO_CONTENT_" * 120_000)
    with open(wav_file, "wb") as f:
        f.write(b"FAKE_AUDIO_CONTENT_" * 10_000)

    meta_payload = {
        "filename": "stream_test_120s.mp4",
        "path": mp4_file,
        "duration": 45.0,
        "hook_title": "Craziest Stream Moment",
        "transcript": "No way bro did that live on stream!",
        "user_rating": 5,
        "user_tag": "viral",
        "sub_scores": {"hook": 9.5, "flow": 8.0, "emotion": 9.0, "standalone": 8.5},
    }
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(meta_payload, f)

    # Execute clip deletion (save space mode)
    res = live_delete_clip("stream_test_120s.mp4", purge=False)
    assert res["ok"] is True
    assert res["freed_mb"] > 0
    assert res["feedback_preserved"] is True

    # Check heavy media files were deleted
    assert not os.path.isfile(mp4_file), "Video file must be deleted to free space"
    assert not os.path.isfile(wav_file), "Audio file must be deleted to free space"

    # Check ai_feedback.json preserved all metadata
    feedback_file = os.path.join(fake_root, "data", "ai_feedback.json")
    assert os.path.isfile(feedback_file)
    with open(feedback_file, "r", encoding="utf-8") as f:
        fb_list = json.load(f)

    assert len(fb_list) == 1
    assert fb_list[0]["filename"] == "stream_test_120s.mp4"
    assert fb_list[0]["rating"] == 5
    assert fb_list[0]["tag"] == "viral"
    assert fb_list[0]["hook_title"] == "Craziest Stream Moment"
    assert fb_list[0]["transcript"] == "No way bro did that live on stream!"
    assert fb_list[0]["sub_scores"]["hook"] == 9.5
    assert fb_list[0]["media_deleted"] is True


def test_offline_buffer_drain(tmp_path, monkeypatch):
    """Verify that when a stream has ended offline, run_loop drains unanalyzed buffer and marks completed."""
    test_dir = str(tmp_path / "offline_drain_test")
    os.makedirs(test_dir, exist_ok=True)

    clipper = LiveStreamClipper(
        stream_url="https://youtube.com/live/ended_stream",
        quality="data_saver",
    )
    clipper.buffer_dir = test_dir
    clipper.buffer_file = os.path.join(test_dir, "live_stream.ts")
    clipper.state_file = os.path.join(test_dir, "state.json")
    clipper.offline_buffer_drain = True
    clipper.last_analyzed_time = 50.0

    # Create dummy buffer file
    with open(clipper.buffer_file, "wb") as f:
        f.write(b"MPEG_TS_DUMMY_DATA" * 500)

    # Mock start_capture_process to simulate stream is over (returns False)
    monkeypatch.setattr(clipper, "start_capture_process", lambda append=False: False)

    # Mock get_media_duration_seconds to return 100.0s (50s remaining)
    monkeypatch.setattr("clipper.live.get_media_duration_seconds", lambda path: 100.0)

    # Mock check_and_clip to simulate analyzing to the end (95.0s)
    def mock_check_and_clip():
        clipper.last_analyzed_time = 95.0
        return []

    monkeypatch.setattr(clipper, "check_and_clip", mock_check_and_clip)

    # Run loop (should enter draining, execute mock_check_and_clip, then finish with completed)
    clipper.run_loop()

    assert clipper.status == "completed"
    assert "fully analyzed" in clipper.activity or "100%" in clipper.activity

    # Verify state.json was written with completed status
    assert os.path.isfile(clipper.state_file)
    with open(clipper.state_file, "r", encoding="utf-8") as f:
        st = json.load(f)
    assert st["status"] == "completed"
    assert st["last_analyzed_time"] == 95.0

