"""Run alignment of all downloaded clips against source stream audio.

Generates matched time ranges [stream_start, stream_end], confidence metrics,
and detailed chunk alignment reports for Phase 1.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from clipper.learning.matcher import match_clip_in_stream, ClipAlignmentResult
from clipper.learning.collector import AUDIO_DIR, MATCHES_DIR, METADATA_DIR


def ensure_stream_wav(stream_id: str) -> Path:
    """Ensure stream .webm is converted to 16 kHz mono WAV."""
    streams_dir = AUDIO_DIR / "streams"
    wav_path = streams_dir / f"{stream_id}.wav"
    webm_path = streams_dir / f"{stream_id}.webm"

    if wav_path.is_file() and wav_path.stat().st_size > 1000000:
        return wav_path

    if not webm_path.is_file():
        raise FileNotFoundError(f"Stream webm not found at {webm_path}")

    print(f"Converting {webm_path} to 16 kHz mono WAV: {wav_path}...")
    t0 = time.time()
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(webm_path),
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(wav_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Conversion complete in {time.time() - t0:.2f}s! Size: {wav_path.stat().st_size:,} bytes")
    return wav_path


def align_clips_for_stream(
    stream_id: str,
    candidate_clip_ids: List[str],
) -> List[ClipAlignmentResult]:
    """Align list of clips against the given stream audio."""
    wav_path = ensure_stream_wav(stream_id)
    clips_dir = AUDIO_DIR / "clips"
    MATCHES_DIR.mkdir(parents=True, exist_ok=True)

    results: List[ClipAlignmentResult] = []
    print(f"\nMatching {len(candidate_clip_ids)} clips against Stream {stream_id}...")

    for idx, cid in enumerate(candidate_clip_ids):
        clip_wav = clips_dir / f"{cid}.wav"
        if not clip_wav.is_file():
            print(f"[{idx+1}/{len(candidate_clip_ids)}] Clip WAV not found for {cid}, skipping")
            continue

        t0 = time.time()
        print(f"[{idx+1}/{len(candidate_clip_ids)}] Matching {cid}...", end=" ", flush=True)
        res = match_clip_in_stream(
            clip_wav=clip_wav,
            stream_wav=wav_path,
            clip_id=cid,
            stream_id=stream_id,
            chunk_sec=4.0,
            chunk_overlap=0.5,
            min_confidence=0.60,
            min_matched_ratio=0.40,
        )
        elapsed = time.time() - t0

        if res.matched:
            print(f"MATCHED! conf={res.confidence:.3f} range=[{res.stream_start:.1f}s - {res.stream_end:.1f}s] ({elapsed:.2f}s)")
        else:
            print(f"NO MATCH ({res.notes}) ({elapsed:.2f}s)")

        results.append(res)
        out_file = MATCHES_DIR / f"{cid}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(res.to_dict(), f, indent=2)

    return results


def build_phase1_report() -> Dict[str, Any]:
    """Aggregate all match results, calculate statistics, and write summary report."""
    with open(METADATA_DIR / "gta6_clips_detailed.json", "r", encoding="utf-8") as f:
        all_clips = json.load(f)

    match_files = list(MATCHES_DIR.glob("*.json"))
    matches_by_id: Dict[str, Dict[str, Any]] = {}
    for mf in match_files:
        if mf.name == "summary.json":
            continue
        try:
            with open(mf, "r", encoding="utf-8") as f:
                d = json.load(f)
                matches_by_id[d["clip_id"]] = d
        except Exception:
            pass

    records = []
    matched_count = 0
    confidences = []

    for c in all_clips:
        cid = c["id"]
        m = matches_by_id.get(cid)
        is_matched = bool(m and m.get("matched"))
        conf = float(m.get("confidence", 0.0)) if m else 0.0
        if is_matched:
            matched_count += 1
            confidences.append(conf)

        records.append({
            "clip_id": cid,
            "title": c.get("title"),
            "upload_date": c.get("upload_date"),
            "duration": c.get("duration"),
            "views": c.get("view_count"),
            "matched": is_matched,
            "confidence": conf,
            "stream_id": m.get("stream_id") if m else None,
            "stream_start": m.get("stream_start") if m else None,
            "stream_end": m.get("stream_end") if m else None,
            "matched_duration": m.get("matched_duration") if m else None,
            "notes": m.get("notes") if m else "Not evaluated yet",
        })

    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_clips_found": len(all_clips),
        "total_clips_evaluated": len(matches_by_id),
        "total_clips_matched": matched_count,
        "match_rate": round(matched_count / max(1, len(matches_by_id)), 4),
        "confidence_stats": {
            "min": round(min(confidences), 4) if confidences else 0.0,
            "max": round(max(confidences), 4) if confidences else 0.0,
            "mean": round(float(sum(confidences) / len(confidences)), 4) if confidences else 0.0,
        },
        "records": records,
    }

    summary_path = MATCHES_DIR / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return summary
