import os, sys
sys.path.insert(0, os.path.abspath("."))

from clipper.learning.collector import get_gta6_clips, fetch_speed_streams, load_speedy_boykins_flat

all_clips = load_speedy_boykins_flat()
print(f"Total Speedy Boykins clips in channel: {len(all_clips)}")

gta = get_gta6_clips(all_clips)
print(f"Total GTA 6 clips found: {len(gta)}")
for i, c in enumerate(gta):
    print(f"{i+1:2d}. [{c['id']}] {c.get('duration')}s | {c.get('view_count'):,} views | {c['title']}")

print("\nFetching/loading Speed streams...")
streams = fetch_speed_streams()
print(f"Total Speed streams: {len(streams)}")
for i, s in enumerate(streams[:15]):
    ls = s.get('live_status')
    dur = s.get('duration') or 0
    dur_h = dur / 3600.0
    print(f"{i+1:2d}. [{s['id']}] status={ls} | {dur_h:.2f}h ({dur}s) | {s.get('title')}")
