"""clipper.py — 100% free stream -> shorts pipeline (chat spikes & audio peaks, local GPU)

Usage:
    python clipper.py vod.mp4 --chat chat.json --top 8
    python clipper.py "https://www.twitch.tv/videos/12345678" --top 5
    python clipper.py "https://www.youtube.com/watch?v=..." --preset neon_green
    python clipper.py --dry-run
"""
import argparse
import json
import logging
import math
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

# Import hardened modules from clipper package
from clipper.captions import DEFAULT_PRESET, escape_ffmpeg_ass_path, load_preset, make_ass
from clipper.cutter import cut_rough_clip, render_vertical_clip
from clipper.detection import detect_audio_loudness_moments, find_moments, spike_moments
from clipper.downloader import prepare_input
from clipper.pipeline import run_pipeline, setup_logging
from clipper.transcription import get_whisper_model, transcribe_clip
from clipper.utils import get_ffmpeg_path, setup_cuda_dlls

# Base ASS header from original script (used as default fallback)
ASS_HEADER = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,88,&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,6,2,2,60,60,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def fmt_ts(t: float) -> str:
    """Format seconds into ASS timestamp H:MM:SS.cs"""
    return f"{int(t // 3600)}:{int((t % 3600) // 60):02d}:{t % 60:05.2f}"


def run_dry_run():
    """Run an automated dry run using synthetic video and chat assets."""
    print("=" * 65)
    print("  RUNNING PIPELINE DRY RUN (Synthetic Test Assets)")
    print("=" * 65)
    from tests.generate_test_assets import generate_synthetic_assets

    test_video, test_chat = generate_synthetic_assets(output_dir="tests/test_data")
    print(f"Generated test assets:\n  Video: {test_video}\n  Chat : {test_chat}")

    print("\nRunning pipeline end-to-end on test assets...")
    clips = run_pipeline(
        vod_input=test_video,
        chat_input=test_chat,
        top=2,
        before=5,
        after=10,
        preset_name="classic_yellow",
        output_dir="tests/test_clips",
    )
    print("\n" + "=" * 65)
    print(f"  DRY RUN SUCCESSFUL: {len(clips)} vertical short clips generated!")
    for c in clips:
        print(f"  - {c['path']} (duration: {c['duration']}s, words: {c['word_count']})")
    print("=" * 65)
    return clips


def main():
    p = argparse.ArgumentParser(
        description="Stream Clipper - Free local VOD to vertical captioned shorts pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("vod", nargs="?", default=None, help="Local video file, Twitch VOD URL, or YouTube URL")
    p.add_argument("--chat", default=None, help="Path to Twitch/YouTube chat JSON (optional; audio fallback used if omitted)")
    p.add_argument("--top", type=int, default=8, help="Number of top moments to clip (default: 8)")
    p.add_argument("--before", type=int, default=20, help="Seconds before hype spike to include (default: 20)")
    p.add_argument("--after", type=int, default=30, help="Seconds after hype spike to include (default: 30)")
    p.add_argument("--window", type=int, default=5, help="Rolling window size in seconds (default: 5)")
    p.add_argument("--z-thresh", type=float, default=2.5, help="Z-score threshold for hype spikes (default: 2.5)")
    p.add_argument("--min-msgs", type=int, default=12, help="Minimum chat messages in window for spike (default: 12)")
    p.add_argument("--model", default="large-v3", help="faster-whisper model (default: large-v3)")
    p.add_argument("--cpu", action="store_true", help="Force CPU mode (int8 Whisper + libx264 encoder)")
    p.add_argument("--preset", default="classic_yellow", help="Caption style preset in presets/ (default: classic_yellow)")
    p.add_argument("--output-dir", default="clips", help="Folder to save rendered clips (default: clips)")
    p.add_argument("--dry-run", action="store_true", help="Execute dry run with synthetic test video and chat")

    a = p.parse_args()

    if a.dry_run:
        run_dry_run()
        return

    if not a.vod:
        p.print_help()
        print("\n[ERROR] Missing required argument: 'vod' (or pass --dry-run to test).")
        sys.exit(1)

    setup_logging()
    setup_cuda_dlls()

    clips = run_pipeline(
        vod_input=a.vod,
        chat_input=a.chat,
        top=a.top,
        before=a.before,
        after=a.after,
        window=a.window,
        z_thresh=a.z_thresh,
        min_msgs=a.min_msgs,
        model=a.model,
        cpu=a.cpu,
        preset_name=a.preset,
        output_dir=a.output_dir,
    )

    print(f"\n[DONE] Successfully created {len(clips)} clips in '{a.output_dir}/'.")


if __name__ == "__main__":
    main()
