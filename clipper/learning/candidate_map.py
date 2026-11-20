import json
from pathlib import Path

with open("data/learning/metadata/gta6_clips_detailed.json", "r", encoding="utf-8") as f:
    clips = json.load(f)

hw_clips = [c for c in clips if c.get("upload_date") == "20261120"]
qs_clips = [c for c in clips if c.get("upload_date") == "20261119"]
other_clips = [c for c in clips if c.get("upload_date") not in ["20261119", "20261120"]]

print(f"Total GTA 6 clips: {len(clips)}")
print(f"  -> Candidate for Stream hwSg39yIBGU (Nov 20): {len(hw_clips)} clips")
print(f"  -> Candidate for Stream qSyo5d-dVzA (Nov 19): {len(qs_clips)} clips")
print(f"  -> Pre-release / Non-stream clips: {len(other_clips)} clips")

print("\nhwSg39yIBGU Candidates:")
for i, c in enumerate(hw_clips):
    print(f"  {i+1:2d}. [{c['id']}] {c.get('duration')}s | {c.get('view_count'):,} views | {c['title'][:55]}")

print("\nqSyo5d-dVzA Candidates:")
for i, c in enumerate(qs_clips):
    print(f"  {i+1:2d}. [{c['id']}] {c.get('duration')}s | {c.get('view_count'):,} views | {c['title'][:55]}")

print("\nPre-release / Reactions (Expected Rejections):")
for i, c in enumerate(other_clips):
    print(f"  {i+1:2d}. [{c['id']}] date={c.get('upload_date')} | {c['title'][:55]}")
