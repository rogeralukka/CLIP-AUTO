"""Match Nov 20 clips against stream hwSg39yIBGU."""

import os, sys
sys.path.insert(0, os.path.abspath("."))

import json
import time
from pathlib import Path
from clipper.learning.fast_align import get_or_compute_stream_envelope, match_single_clip
from clipper.learning.collector import AUDIO_DIR, MATCHES_DIR, METADATA_DIR

streams_dir = AUDIO_DIR / "streams"
clips_dir = AUDIO_DIR / "clips"
MATCHES_DIR.mkdir(parents=True, exist_ok=True)

hw_wav = streams_dir / "hwSg39yIBGU.wav"
print(f"Loading/preparing Stream hwSg39yIBGU ({hw_wav.stat().st_size / 1e9:.2f} GB)...", flush=True)
t0 = time.time()
env_hw, pos_hw, dur_hw = get_or_compute_stream_envelope(hw_wav, env_hz=500)
print(f"Stream hwSg39yIBGU envelope ready in {time.time() - t0:.2f}s! ({dur_hw / 3600:.2f}h, {len(env_hw):,} pts)", flush=True)

with open(METADATA_DIR / "gta6_clips_detailed.json", "r", encoding="utf-8") as f:
    clips = json.load(f)

hw_clips = [c for c in clips if c.get("upload_date") == "20261120"]
print(f"\nMatching {len(hw_clips)} clips against hwSg39yIBGU...", flush=True)

matched_count = 0
for idx, c in enumerate(hw_clips):
    cid = c["id"]
    clip_wav = clips_dir / f"{cid}.wav"
    if not clip_wav.is_file():
        print(f"[{idx+1}/{len(hw_clips)}] Clip {cid} WAV not found, skipping", flush=True)
        continue

    t_m = time.time()
    res = match_single_clip(
        clip_wav=clip_wav,
        stream_wav=hw_wav,
        stream_env=env_hw,
        data_pos=pos_hw,
        stream_dur=dur_hw,
        clip_id=cid,
        stream_id="hwSg39yIBGU",
        env_hz=500,
        chunk_sec=4.0,
        chunk_overlap=0.5,
        min_confidence=0.55,
        min_matched_ratio=0.35,
    )
    elapsed = time.time() - t_m

    status = "MATCHED!" if res.matched else "FAILED"
    if res.matched:
        matched_count += 1
        h_start = res.stream_start / 3600.0
        h_end = res.stream_end / 3600.0
        print(f"[{idx+1}/{len(hw_clips)}] {status} [{cid}] conf={res.confidence:.3f} | range=[{res.stream_start:.1f}s - {res.stream_end:.1f}s] ({h_start:.2f}h - {h_end:.2f}h) | chunks={len(res.chunk_matches)} ({elapsed:.2f}s)", flush=True)
    else:
        print(f"[{idx+1}/{len(hw_clips)}] {status} [{cid}] conf={res.confidence:.3f} ({res.notes}) ({elapsed:.2f}s)", flush=True)

    with open(MATCHES_DIR / f"{cid}.json", "w", encoding="utf-8") as f:
        json.dump(res.to_dict(), f, indent=2)

print(f"\nhwSg39yIBGU matching complete: {matched_count}/{len(hw_clips)} matched ({matched_count / len(hw_clips) * 100:.1f}%)", flush=True)
