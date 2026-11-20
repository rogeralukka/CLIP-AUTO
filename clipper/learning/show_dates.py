import json, glob
files = sorted(glob.glob('data/learning/metadata/details/*.json'))
print(f"Total fetched: {len(files)}")
for f in files:
    d = json.load(open(f, encoding='utf-8'))
    print(f"{d.get('id')} | {d.get('upload_date')} | {d.get('duration')}s | {d.get('title')[:60]}")
