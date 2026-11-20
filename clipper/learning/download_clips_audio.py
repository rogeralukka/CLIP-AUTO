import os, sys, time
sys.path.insert(0, os.path.abspath("."))
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from clipper.learning.collector import download_audio_16k_mono, AUDIO_DIR

import json

with open("data/learning/metadata/gta6_clips_detailed.json", "r", encoding="utf-8") as f:
    detailed = json.load(f)

# Filter to real stream gameplay clips (20261119 and 20261120)
clip_ids = [c["id"] for c in detailed if c.get("upload_date") in ["20261119", "20261120"]]

clips_dir = AUDIO_DIR / "clips"
clips_dir.mkdir(parents=True, exist_ok=True)

def fetch_one(cid: str):
    out_wav = clips_dir / f"{cid}.wav"
    t0 = time.time()
    res = download_audio_16k_mono(cid, out_wav)
    sz = res.stat().st_size if res.is_file() else 0
    return cid, res.is_file(), sz, time.time() - t0

print(f"Downloading audio for {len(clip_ids)} clips...")
t_all = time.time()
with ThreadPoolExecutor(max_workers=4) as executor:
    futures = [executor.submit(fetch_one, cid) for cid in clip_ids]
    for fut in as_completed(futures):
        cid, ok, sz, dur = fut.result()
        print(f"Clip {cid} -> ok={ok}, {sz:,} bytes in {dur:.2f}s")

print(f"All clips audio downloaded in {time.time() - t_all:.2f}s!")
