import os, sys, time
sys.path.insert(0, os.path.abspath("."))
from pathlib import Path
from clipper.learning.collector import download_audio_16k_mono

clip_id = "zXC3ec34JXs"
out_wav = Path(f"data/learning/audio/clips/{clip_id}.wav")

t0 = time.time()
print(f"Downloading clip {clip_id} audio to {out_wav}...")
res = download_audio_16k_mono(clip_id, out_wav)
elapsed = time.time() - t0
print(f"Finished in {elapsed:.2f}s! File exists: {res.is_file()}, size: {res.stat().st_size:,} bytes")
