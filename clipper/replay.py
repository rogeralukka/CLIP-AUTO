"""Replay test harness for stream clipping engines.

Runs detection, transcription, judging, and ranking offline on slices of saved streams
or live buffers without modifying live recordings. Supports side-by-side comparison
of 'old' vs 'new' engines and correlates results with creator feedback in data/ai_feedback.json.

Usage:
    python -m clipper.replay <file> --start MIN --length MIN --engine old|new [--compare]
"""
import argparse
import datetime
import json
import logging
import math
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from .config import load_config
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.replay")


def extract_audio_slice(
    source_file: str,
    start_sec: float,
    duration_sec: float,
    output_wav: str,
) -> bool:
    """Extract a 16kHz mono WAV slice using ffmpeg. Read-only on source_file."""
    if not os.path.isfile(source_file):
        raise FileNotFoundError(f"Source file not found: {source_file}")

    os.makedirs(os.path.dirname(os.path.abspath(output_wav)), exist_ok=True)
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"

    cmd = [
        ffmpeg_bin, "-y", "-v", "error",
        "-ss", f"{start_sec:.2f}",
        "-t", f"{duration_sec:.2f}",
        "-i", source_file,
        "-vn", "-ac", "1", "-ar", "16000",
        output_wav,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return res.returncode == 0 and os.path.isfile(output_wav)
    except Exception as e:
        logger.error(f"FFmpeg slice extraction failed: {e}")
        return False


def load_creator_feedback() -> List[Dict[str, Any]]:
    """Load user ratings and feedback from data/ai_feedback.json."""
    feedback_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "ai_feedback.json",
    )
    if not os.path.isfile(feedback_file):
        return []
    try:
        with open(feedback_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Could not load ai_feedback.json: {e}")
        return []


def parse_timestamp_from_filename(filename: str) -> Optional[float]:
    """Extract timestamp in seconds from clip filename, e.g. '..._15802s.mp4' -> 15802.0."""
    m = re.search(r"_(\d+)s(?:\.mp4)?$", filename)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def correlate_with_feedback(
    candidates: List[Dict[str, Any]],
    start_sec: float,
    length_sec: float,
    source_file: str,
) -> Dict[str, Any]:
    """Correlate candidate moments and judge scores with user feedback in data/ai_feedback.json."""
    feedback_items = load_creator_feedback()
    end_sec = start_sec + length_sec

    # Filter feedback items that fall within this stream slice
    relevant_feedback: List[Dict[str, Any]] = []
    for item in feedback_items:
        ts = item.get("stream_time_seconds")
        if ts is None:
            ts = parse_timestamp_from_filename(item.get("filename", ""))
        if ts is not None and start_sec <= ts <= end_sec:
            item_copy = dict(item)
            item_copy["parsed_stream_sec"] = ts
            item_copy["slice_rel_sec"] = ts - start_sec
            relevant_feedback.append(item_copy)

    total_relevant = len(relevant_feedback)
    if total_relevant == 0:
        return {
            "total_user_ratings_in_slice": 0,
            "matched_candidates": 0,
            "recall": 0.0,
            "alignment_rate": 0.0,
            "details": [],
            "note": "No user ratings in data/ai_feedback.json overlapped with this slice window.",
        }

    matched_count = 0
    correct_decisions = 0
    score_pairs: List[Tuple[float, float]] = []
    details = []

    for item in relevant_feedback:
        user_rating = float(item.get("rating", 3))  # 1-5 scale
        target_sec = item["slice_rel_sec"]
        user_is_positive = user_rating >= 4.0
        user_is_negative = user_rating <= 2.0

        # Find closest overlapping candidate (within 30s)
        best_match = None
        min_dist = float("inf")
        for cand in candidates:
            cand_mid = (cand["start"] + cand["end"]) / 2.0
            dist = abs(cand_mid - target_sec)
            if dist <= 35.0 and dist < min_dist:
                min_dist = dist
                best_match = cand

        if best_match:
            matched_count += 1
            cand_score = float(best_match.get("score", 0.0))
            is_accepted = best_match.get("status") == "accepted"
            score_pairs.append((user_rating, cand_score))

            aligned = (user_is_positive and is_accepted) or (user_is_negative and not is_accepted)
            if aligned:
                correct_decisions += 1

            details.append({
                "stream_sec": item["parsed_stream_sec"],
                "user_rating": user_rating,
                "user_tag": item.get("tag", ""),
                "matched_candidate_start": best_match["start"],
                "candidate_score": cand_score,
                "candidate_status": best_match.get("status"),
                "aligned": aligned,
            })
        else:
            details.append({
                "stream_sec": item["parsed_stream_sec"],
                "user_rating": user_rating,
                "user_tag": item.get("tag", ""),
                "matched_candidate_start": None,
                "candidate_score": None,
                "candidate_status": "missed",
                "aligned": not user_is_positive,  # if user disliked it and engine missed it, that's fine
            })

    # Pearson correlation if >= 2 points
    pearson_r = None
    if len(score_pairs) >= 2:
        u_vals = [p[0] for p in score_pairs]
        c_vals = [p[1] for p in score_pairs]
        u_mean = sum(u_vals) / len(u_vals)
        c_mean = sum(c_vals) / len(c_vals)
        num = sum((u - u_mean) * (c - c_mean) for u, c in score_pairs)
        den = math.sqrt(sum((u - u_mean) ** 2 for u in u_vals) * sum((c - c_mean) ** 2 for c in c_vals))
        if den > 1e-6:
            pearson_r = round(num / den, 3)

    return {
        "total_user_ratings_in_slice": total_relevant,
        "matched_candidates": matched_count,
        "recall": round(matched_count / total_relevant, 2) if total_relevant else 0.0,
        "alignment_rate": round(correct_decisions / total_relevant, 2) if total_relevant else 0.0,
        "pearson_correlation": pearson_r,
        "details": details,
    }


def run_old_engine(
    slice_wav: str,
    slice_duration: float,
    sensitivity: float = 2.0,
    use_llm: bool = True,
    endpoint: str = "http://localhost:11434",
    model_name: str = "qwen3:14b",
) -> List[Dict[str, Any]]:
    """Run the legacy composite hype-curve detector and LLM veto judge."""
    from .detection import find_moments
    from .judge import is_ollama_available, judge_single_moment
    from .transcription import get_transcript_text_in_window, transcribe_full_audio, unload_whisper_model

    logger.info(f"Running OLD engine on slice ({slice_duration:.1f}s)...")

    # 1. Candidate detection using find_moments
    moments = find_moments(
        comments=None,
        video_path=slice_wav,
        z_thresh=sensitivity,
        before=20,
        after=30,
        window=5,
        min_clip_length=20.0,
        max_clip_length=70.0,
    )

    logger.info(f"OLD engine found {len(moments)} initial candidate moments.")
    if not moments:
        return []

    # 2. Transcribe the slice once for word lookup (cached to disk)
    transcript_cache = slice_wav.replace(".wav", "_transcript.json")
    transcript_data = transcribe_full_audio(slice_wav, use_cpu=False, output_json=transcript_cache)
    words = transcript_data.get("words", [])

    candidates: List[Dict[str, Any]] = []

    # Check LLM availability
    ollama_ok = False
    if use_llm:
        ollama_ok, _ = is_ollama_available(endpoint)
        if ollama_ok:
            unload_whisper_model()

    for idx, m in enumerate(moments):
        start_s = float(m["start"])
        end_s = float(m["end"])
        cand_words = [w for w in words if start_s <= w.get("start", 0) <= end_s]
        cand_text = " ".join(w.get("word", "") for w in cand_words).strip()
        dur = end_s - start_s

        cand_record = {
            "id": f"old_cand_{idx + 1}",
            "start": round(start_s, 2),
            "end": round(end_s, 2),
            "duration": round(dur, 2),
            "signals": m.get("breakdown", {"streamer_loudness": m.get("score", 0.0)}),
            "raw_detection_score": round(m.get("score", 0.0), 2),
            "transcript": cand_text[:120] + ("..." if len(cand_text) > 120 else ""),
            "hook_title": "",
            "score": round(m.get("score", 0.0), 2),
            "status": "accepted",
            "decision_reason": "Accepted by loudness curve",
        }

        # 3. LLM Judge Veto (Old style: reject if score < 4.5 or (score < 5.5 and not payoff))
        if use_llm and ollama_ok:
            try:
                eval_res = judge_single_moment(
                    moment=m,
                    transcript=cand_text,
                    chat_sample=[],
                    endpoint=endpoint,
                    model=model_name,
                )
                if eval_res:
                    judge_score = float(eval_res.get("score", 5.0))
                    standalone = bool(eval_res.get("standalone", True))
                    payoff = bool(eval_res.get("payoff_inside", True))
                    cand_record["score"] = round(judge_score, 2)
                    cand_record["standalone"] = standalone
                    cand_record["payoff_inside"] = payoff
                    cand_record["hook_title"] = eval_res.get("hook_title", "")

                    if judge_score < 4.5 or (judge_score < 5.5 and not payoff):
                        cand_record["status"] = "rejected"
                        cand_record["decision_reason"] = (
                            eval_res.get("reason")
                            or f"Low judge score ({judge_score:.1f}/10) or missing payoff"
                        )
                    else:
                        cand_record["status"] = "accepted"
                        cand_record["decision_reason"] = (
                            eval_res.get("reason") or f"Approved by judge ({judge_score:.1f}/10)"
                        )
            except Exception as e:
                logger.warning(f"Judge evaluation failed for moment {idx + 1}: {e}")

        candidates.append(cand_record)

    return candidates


def run_new_engine(
    slice_wav: str,
    slice_duration: float,
    source_video: Optional[str] = None,
    slice_offset_sec: float = 0.0,
    sensitivity: float = 2.0,
    max_clips_per_hour: int = 10,
    endpoint: str = "http://localhost:11434",
    model_name: str = "qwen3:14b",
) -> List[Dict[str, Any]]:
    """Run the NEW story-hunter + pooled candidate engine with 4-pillar ranking."""
    try:
        from .story import hunt_story_candidates_and_pool
        return hunt_story_candidates_and_pool(
            slice_wav=slice_wav,
            slice_duration=slice_duration,
            source_video=source_video,
            slice_offset_sec=slice_offset_sec,
            sensitivity=sensitivity,
            max_clips_per_hour=max_clips_per_hour,
            endpoint=endpoint,
            model_name=model_name,
        )
    except ImportError:
        logger.warning("clipper.story module not yet available; run Step 2 to enable new engine.")
        return []


def format_comparison_table(
    old_candidates: List[Dict[str, Any]],
    new_candidates: List[Dict[str, Any]],
    start_sec: float,
) -> str:
    """Format side-by-side terminal comparison table."""
    lines = []
    lines.append("\n" + "=" * 90)
    lines.append(f"{'ENGINE COMPARISON SUMMARY':^90}")
    lines.append("=" * 90)

    old_accepted = [c for c in old_candidates if c.get("status") == "accepted"]
    new_accepted = [c for c in new_candidates if c.get("status") == "accepted"]

    lines.append(f"Old Engine: {len(old_candidates)} candidates found, {len(old_accepted)} accepted.")
    lines.append(f"New Engine: {len(new_candidates)} candidates found, {len(new_accepted)} accepted.")
    lines.append("-" * 90)

    # Detailed candidate listing
    lines.append(f"{'Engine':<6} | {'Time (rel/abs)':<18} | {'Score':<6} | {'Status':<10} | {'Hook Title / Reason':<42}")
    lines.append("-" * 90)

    for c in old_candidates:
        abs_t = start_sec + c["start"]
        t_str = f"{c['start']:.0f}s ({abs_t:.0f}s)"
        score_str = f"{c.get('score', 0.0):.1f}"
        status_str = c.get("status", "unknown").upper()
        info = c.get("hook_title") or c.get("decision_reason") or ""
        lines.append(f"{'OLD':<6} | {t_str:<18} | {score_str:<6} | {status_str:<10} | {info[:42]:<42}")

    if new_candidates:
        lines.append("-" * 90)
        for c in new_candidates:
            abs_t = start_sec + c["start"]
            t_str = f"{c['start']:.0f}s ({abs_t:.0f}s)"
            score_str = f"{c.get('score', 0.0):.1f}"
            status_str = c.get("status", "unknown").upper()
            info = c.get("hook_title") or c.get("decision_reason") or ""
            lines.append(f"{'NEW':<6} | {t_str:<18} | {score_str:<6} | {status_str:<10} | {info[:42]:<42}")

    lines.append("=" * 90 + "\n")
    return "\n".join(lines)


def replay_slice(
    source_file: str,
    start_min: float,
    length_min: float = 30.0,
    engine: str = "old",
    compare: bool = False,
    output_dir: str = "downloads/replay",
    sensitivity: float = 2.0,
    max_clips_per_hour: int = 10,
    endpoint: str = "http://localhost:11434",
    model_name: str = "qwen3:14b",
) -> Dict[str, Any]:
    """Execute offline replay test on a slice of a stream/buffer."""
    start_sec = start_min * 60.0
    duration_sec = length_min * 60.0

    os.makedirs(output_dir, exist_ok=True)
    slice_id = f"slice_{int(start_sec)}s_{int(duration_sec)}s"
    slice_wav = os.path.join(output_dir, "slices", f"{slice_id}.wav")

    logger.info(f"Extracting slice from {source_file} [start={start_min:.1f}m, length={length_min:.1f}m]...")
    ok = extract_audio_slice(source_file, start_sec, duration_sec, slice_wav)
    if not ok:
        raise RuntimeError(f"Failed to extract audio slice from {source_file}")

    report: Dict[str, Any] = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_file": source_file,
        "slice_start_min": start_min,
        "slice_length_min": length_min,
        "slice_start_sec": start_sec,
        "slice_duration_sec": duration_sec,
        "engine_mode": "compare" if compare else engine,
    }

    old_results: List[Dict[str, Any]] = []
    new_results: List[Dict[str, Any]] = []

    if compare or engine == "old":
        old_results = run_old_engine(
            slice_wav=slice_wav,
            slice_duration=duration_sec,
            sensitivity=sensitivity,
            endpoint=endpoint,
            model_name=model_name,
        )
        report["old_engine"] = {
            "candidates": old_results,
            "total_candidates": len(old_results),
            "accepted_count": sum(1 for c in old_results if c.get("status") == "accepted"),
            "feedback_correlation": correlate_with_feedback(old_results, start_sec, duration_sec, source_file),
        }

    if compare or engine == "new":
        new_results = run_new_engine(
            slice_wav=slice_wav,
            slice_duration=duration_sec,
            source_video=source_file,
            slice_offset_sec=start_sec,
            sensitivity=sensitivity,
            max_clips_per_hour=max_clips_per_hour,
            endpoint=endpoint,
            model_name=model_name,
        )
        report["new_engine"] = {
            "candidates": new_results,
            "total_candidates": len(new_results),
            "accepted_count": sum(1 for c in new_results if c.get("status") == "accepted"),
            "feedback_correlation": correlate_with_feedback(new_results, start_sec, duration_sec, source_file),
        }

    # Print terminal output
    if compare:
        table_str = format_comparison_table(old_results, new_results, start_sec)
        print(table_str)
    else:
        active_results = new_results if engine == "new" else old_results
        print(f"\n--- REPLAY RESULTS ({engine.upper()} ENGINE) ---")
        print(f"Total Candidates: {len(active_results)}")
        accepted = [c for c in active_results if c.get("status") == "accepted"]
        print(f"Accepted Clips: {len(accepted)}")
        for c in active_results:
            print(f"  [{c['start']:.1f}s - {c['end']:.1f}s] Score: {c.get('score', 0):.1f} | {c.get('status').upper()} | {c.get('hook_title') or c.get('decision_reason')}")

    # Correlate summary print
    primary_corr = (report.get("new_engine") or report.get("old_engine", {})).get("feedback_correlation", {})
    if primary_corr.get("total_user_ratings_in_slice", 0) > 0:
        print(f"\n--- CREATOR FEEDBACK CORRELATION ---")
        print(f"User Ratings in Window: {primary_corr['total_user_ratings_in_slice']}")
        print(f"Candidate Recall: {primary_corr['recall'] * 100:.1f}%")
        print(f"Decision Alignment: {primary_corr['alignment_rate'] * 100:.1f}%")
        if primary_corr.get("pearson_correlation") is not None:
            print(f"Score Correlation (r): {primary_corr['pearson_correlation']}")

    # Save JSON report
    report_filename = f"replay_report_{int(start_sec)}s_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path = os.path.join(output_dir, report_filename)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    logger.info(f"Replay report written to {report_path}")
    report["report_path"] = report_path

    return report


def main():
    parser = argparse.ArgumentParser(description="Replay test harness for stream clipping engines.")
    parser.add_argument("file", help="Path to buffer or VOD file (e.g. downloads/live_buffer/.../live_stream.ts)")
    parser.add_argument("--start", type=float, required=True, help="Start time in minutes")
    parser.add_argument("--length", type=float, default=30.0, help="Slice length in minutes (default: 30)")
    parser.add_argument("--engine", choices=["old", "new"], default="old", help="Engine to run (default: old)")
    parser.add_argument("--compare", action="store_true", help="Run both old and new engines side-by-side")
    parser.add_argument("--output-dir", default="downloads/replay", help="Directory for output slices and reports")
    parser.add_argument("--sensitivity", type=float, default=2.0, help="Sensitivity threshold (default: 2.0)")
    parser.add_argument("--max-clips-per-hour", type=int, default=10, help="Max clips per hour cap (default: 10)")
    parser.add_argument("--endpoint", default="http://localhost:11434", help="Ollama endpoint")
    parser.add_argument("--model", default="qwen3:14b", help="Ollama model name (default: qwen3:14b)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    replay_slice(
        source_file=args.file,
        start_min=args.start,
        length_min=args.length,
        engine=args.engine,
        compare=args.compare,
        output_dir=args.output_dir,
        sensitivity=args.sensitivity,
        max_clips_per_hour=args.max_clips_per_hour,
        endpoint=args.endpoint,
        model_name=args.model,
    )


if __name__ == "__main__":
    main()
