import os, sys, json
sys.path.insert(0, os.path.abspath("."))

from clipper.learning.collector import get_gta6_clips, fetch_video_details, METADATA_DIR

clips = get_gta6_clips()
print(f"Fetching details for {len(clips)} GTA 6 clips...")

clip_details = []
for i, c in enumerate(clips):
    cid = c["id"]
    try:
        details = fetch_video_details(cid)
        clip_details.append(details)
        print(f"[{i+1}/{len(clips)}] {cid} -> {details.get('upload_date')} | {details.get('duration')}s | {details.get('title')[:45]}")
    except Exception as e:
        print(f"[{i+1}/{len(clips)}] Error fetching {cid}: {e}")

out_path = METADATA_DIR / "gta6_clips_detailed.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(clip_details, f, indent=2)

print(f"Saved {len(clip_details)} detailed clips to {out_path}")

# Fetch source streams details
for sid in ["hwSg39yIBGU", "qSyo5d-dVzA", "o0UeucVxYCA", "Kush1FqnhbU"]:
    try:
        sdet = fetch_video_details(sid)
        print(f"VOD {sid} -> upload_date: {sdet.get('upload_date')} | dur: {sdet.get('duration')}s | {sdet.get('title')}")
    except Exception as e:
        print(f"VOD {sid} error: {e}")
