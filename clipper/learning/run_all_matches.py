"""Execute full alignment across all Speedy Boykins clips and source streams."""

from __future__ import annotations

import json
import time
from pathlib import Path

from clipper.learning.fast_align import (
    get_or_compute_stream_envelope,
    match_single_clip,
)
from clipper.learning.collector import AUDIO_DIR, MATCHES_DIR, METADATA_DIR


def main():
    print("=== Starting CLIP AUTO Phase 1 Alignment ===")
    t_start = time.time()

    streams_dir = AUDIO_DIR / "streams"
    clips_dir = AUDIO_DIR / "clips"
    MATCHES_DIR.mkdir(parents=True, exist_ok=True)

    with open(METADATA_DIR / "gta6_clips_detailed.json", "r", encoding="utf-8") as f:
        clips = json.load(f)

    # Stream 1: hwSg39yIBGU (Nov 20)
    hw_wav = streams_dir / "hwSg39yIBGU.wav"
    # Stream 2: qSyo5d-dVzA (Nov 19)
    qs_wav = streams_dir / "qSyo5d-dVzA.wav"

    streams_ready = {}
    if qs_wav.is_file() and qs_wav.stat().st_size > 100000000:
        print(f"Loading/preparing Stream qSyo5d-dVzA ({qs_wav.stat().st_size / 1e9:.2f} GB)...")
        env_qs, pos_qs, dur_qs = get_or_compute_stream_envelope(qs_wav, env_hz=500)
        streams_ready["qSyo5d-dVzA"] = (qs_wav, env_qs, pos_qs, dur_qs)

    if hw_wav.is_file() and hw_wav.stat().st_size > 100000000:
        print(f"Loading/preparing Stream hwSg39yIBGU ({hw_wav.stat().st_size / 1e9:.2f} GB)...")
        env_hw, pos_hw, dur_hw = get_or_compute_stream_envelope(hw_wav, env_hz=500)
        streams_ready["hwSg39yIBGU"] = (hw_wav, env_hw, pos_hw, dur_hw)

    print(f"Streams ready for alignment: {list(streams_ready.keys())}")

    results = []
    matched_count = 0

    for i, c in enumerate(clips):
        cid = c["id"]
        title = c["title"]
        up_date = c.get("upload_date")
        clip_wav = clips_dir / f"{cid}.wav"

        print(f"\n[{i+1}/{len(clips)}] Clip: [{cid}] {title[:45]} ({c.get('duration')}s, {up_date})")

        if not clip_wav.is_file():
            print("  -> Clip audio not found on disk, skipping.")
            results.append({
                "clip_id": cid,
                "title": title,
                "upload_date": up_date,
                "views": c.get("view_count"),
                "duration": c.get("duration"),
                "matched": False,
                "confidence": 0.0,
                "notes": "Clip WAV file missing",
            })
            continue

        # Choose primary candidate stream based on date
        primary_sid = "hwSg39yIBGU" if up_date == "20261120" else "qSyo5d-dVzA"
        secondary_sid = "qSyo5d-dVzA" if primary_sid == "hwSg39yIBGU" else "hwSg39yIBGU"

        best_res = None

        for sid in [primary_sid, secondary_sid]:
            if sid not in streams_ready:
                continue
            s_wav, s_env, s_pos, s_dur = streams_ready[sid]
            t0 = time.time()
            res = match_single_clip(
                clip_wav=clip_wav,
                stream_wav=s_wav,
                stream_env=s_env,
                data_pos=s_pos,
                stream_dur=s_dur,
                clip_id=cid,
                stream_id=sid,
                env_hz=500,
                chunk_sec=4.0,
                chunk_overlap=0.5,
                min_confidence=0.55,
                min_matched_ratio=0.35,
            )
            elapsed = time.time() - t0

            if res.matched:
                print(f"  -> MATCHED in {sid}! conf={res.confidence:.3f} range=[{res.stream_start:.1f}s - {res.stream_end:.1f}s] in {elapsed:.2f}s")
                best_res = res
                break
            else:
                if best_res is None or res.confidence > best_res.confidence:
                    best_res = res

        if best_res and best_res.matched:
            matched_count += 1
            rec = {
                "clip_id": cid,
                "title": title,
                "upload_date": up_date,
                "views": c.get("view_count"),
                "duration": c.get("duration"),
                "matched": True,
                "confidence": best_res.confidence,
                "stream_id": best_res.stream_id,
                "stream_start": best_res.stream_start,
                "stream_end": best_res.stream_end,
                "matched_duration": best_res.matched_duration,
                "chunk_count": len(best_res.chunk_matches),
                "notes": best_res.notes,
            }
            # Save individual match JSON
            with open(MATCHES_DIR / f"{cid}.json", "w", encoding="utf-8") as f:
                json.dump(best_res.to_dict(), f, indent=2)
        else:
            rec = {
                "clip_id": cid,
                "title": title,
                "upload_date": up_date,
                "views": c.get("view_count"),
                "duration": c.get("duration"),
                "matched": False,
                "confidence": best_res.confidence if best_res else 0.0,
                "stream_id": best_res.stream_id if best_res else None,
                "stream_start": 0.0,
                "stream_end": 0.0,
                "matched_duration": 0.0,
                "notes": best_res.notes if best_res else "No stream available",
            }
            if best_res:
                with open(MATCHES_DIR / f"{cid}.json", "w", encoding="utf-8") as f:
                    json.dump(best_res.to_dict(), f, indent=2)

        results.append(rec)

    # Master summary
    matched_confs = [r["confidence"] for r in results if r["matched"]]
    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_clips_found": len(clips),
        "total_clips_evaluated": len(results),
        "total_clips_matched": matched_count,
        "match_rate": round(matched_count / max(1, len(results)), 4),
        "streams_covered": list(streams_ready.keys()),
        "confidence_stats": {
            "min": round(min(matched_confs), 4) if matched_confs else 0.0,
            "max": round(max(matched_confs), 4) if matched_confs else 0.0,
            "mean": round(float(sum(matched_confs) / len(matched_confs)), 4) if matched_confs else 0.0,
            "median": round(float(sorted(matched_confs)[len(matched_confs) // 2]), 4) if matched_confs else 0.0,
        },
        "records": results,
    }

    summary_path = MATCHES_DIR / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n=== Phase 1 Alignment Complete ===")
    print(f"Total Clips Evaluated: {len(results)}")
    print(f"Clips Matched: {matched_count}/{len(results)} ({summary['match_rate'] * 100:.1f}%)")
    print(f"Elapsed Time: {time.time() - t_start:.2f}s")
    print(f"Summary saved to: {summary_path}")


if __name__ == "__main__":
    main()
