"""Match Nov 19 clips against stream qSyo5d-dVzA."""

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

qs_wav = streams_dir / "qSyo5d-dVzA.wav"
print(f"Loading/preparing Stream qSyo5d-dVzA ({qs_wav.stat().st_size / 1e9:.2f} GB)...")
t0 = time.time()
env_qs, pos_qs, dur_qs = get_or_compute_stream_envelope(qs_wav, env_hz=500)
print(f"Stream qSyo5d-dVzA envelope ready in {time.time() - t0:.2f}s! ({dur_qs / 3600:.2f}h, {len(env_qs):,} pts)")

with open(METADATA_DIR / "gta6_clips_detailed.json", "r", encoding="utf-8") as f:
    clips = json.load(f)

qs_clips = [c for c in clips if c.get("upload_date") == "20261119"]
print(f"\nMatching {len(qs_clips)} clips against qSyo5d-dVzA...")

matched_count = 0
for idx, c in enumerate(qs_clips):
    cid = c["id"]
    clip_wav = clips_dir / f"{cid}.wav"
    if not clip_wav.is_file():
        print(f"[{idx+1}/{len(qs_clips)}] Clip {cid} WAV not found, skipping")
        continue

    t_m = time.time()
    res = match_single_clip(
        clip_wav=clip_wav,
        stream_wav=qs_wav,
        stream_env=env_qs,
        data_pos=pos_qs,
        stream_dur=dur_qs,
        clip_id=cid,
        stream_id="qSyo5d-dVzA",
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
        print(f"[{idx+1}/{len(qs_clips)}] {status} [{cid}] conf={res.confidence:.3f} | range=[{res.stream_start:.1f}s - {res.stream_end:.1f}s] ({h_start:.2f}h - {h_end:.2f}h) | chunks={len(res.chunk_matches)} ({elapsed:.2f}s)", flush=True)
    else:
        print(f"[{idx+1}/{len(qs_clips)}] {status} [{cid}] conf={res.confidence:.3f} ({res.notes}) ({elapsed:.2f}s)", flush=True)

    with open(MATCHES_DIR / f"{cid}.json", "w", encoding="utf-8") as f:
        json.dump(res.to_dict(), f, indent=2)

print(f"\nqSyo5d-dVzA matching complete: {matched_count}/{len(qs_clips)} matched ({matched_count / len(qs_clips) * 100:.1f}%)")
