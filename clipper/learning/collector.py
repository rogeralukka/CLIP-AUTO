"""Speedy Boykins clips & Speed stream VOD collector.

Fetches video metadata, filters by category (e.g. GTA 6, games, IRL),
matches clips with source stream VODs, and handles downloading audio & chat replays.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import yt_dlp

BASE_LEARNING_DIR = Path("data/learning")
METADATA_DIR = BASE_LEARNING_DIR / "metadata"
AUDIO_DIR = BASE_LEARNING_DIR / "audio"
CHAT_DIR = BASE_LEARNING_DIR / "chat"
MATCHES_DIR = BASE_LEARNING_DIR / "matches"

SPEEDY_BOYKINS_URL = "https://www.youtube.com/channel/UCd09ztChTkJ9dlY6RLawSog/videos"
SPEED_STREAMS_URL = "https://www.youtube.com/@IShowSpeed/streams"

NODE_PATH = r"C:\Program Files\nodejs\node.exe"


def get_ydl_opts(flat: bool = True, quiet: bool = True) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": quiet,
        "no_warnings": True,
    }
    if flat:
        opts["extract_flat"] = True
    if os.path.isfile(NODE_PATH):
        opts["js_runtimes"] = {"node": {"path": NODE_PATH}}
    return opts


def load_speedy_boykins_flat() -> List[Dict[str, Any]]:
    path = METADATA_DIR / "speedy_boykins_flat.json"
    if not path.is_file():
        raise FileNotFoundError(f"Flat metadata not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_gta6_clips(videos: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    if videos is None:
        videos = load_speedy_boykins_flat()
    gta6: List[Dict[str, Any]] = []
    for v in videos:
        title = (v.get("title") or "").lower()
        if "gta 6" in title or "gta6" in title or "gta vi" in title:
            gta6.append(v)
    return gta6


def fetch_speed_streams(force_refresh: bool = False) -> List[Dict[str, Any]]:
    path = METADATA_DIR / "speed_streams_flat.json"
    if path.is_file() and not force_refresh:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    opts = get_ydl_opts(flat=True, quiet=False)
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(SPEED_STREAMS_URL, download=False)
        entries = res.get("entries", [])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        return entries


def fetch_video_details(video_id: str) -> Dict[str, Any]:
    """Fetch complete metadata for a single video (including upload_date, timestamp, etc.)."""
    cache_path = METADATA_DIR / "details" / f"{video_id}.json"
    if cache_path.is_file():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    opts = get_ydl_opts(flat=False, quiet=True)
    opts["skip_download"] = True
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        # Filter down large raw blobs
        cleaned = {
            "id": info.get("id"),
            "title": info.get("title"),
            "duration": info.get("duration"),
            "view_count": info.get("view_count"),
            "upload_date": info.get("upload_date"),
            "timestamp": info.get("timestamp"),
            "release_timestamp": info.get("release_timestamp"),
            "description": info.get("description"),
            "channel_id": info.get("channel_id"),
            "channel": info.get("channel"),
            "was_live": info.get("was_live"),
            "is_live": info.get("is_live"),
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2)
        return cleaned


def download_audio_16k_mono(url_or_id: str, output_path: Path) -> Path:
    """Download audio only converted to 16 kHz mono WAV using yt-dlp and ffmpeg."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_file() and output_path.stat().st_size > 1000:
        return output_path

    if not url_or_id.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url_or_id}"
    else:
        url = url_or_id

    # Use ffmpeg via yt-dlp postprocessors or direct download
    # Downloading as 16kHz mono directly saves space and CPU
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_path.with_suffix("")),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
        }],
        "postprocessor_args": [
            "-ar", "16000",
            "-ac", "1",
        ],
        "quiet": True,
        "no_warnings": True,
    }
    if os.path.isfile(NODE_PATH):
        ydl_opts["js_runtimes"] = {"node": {"path": NODE_PATH}}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    final_wav = output_path.with_suffix(".wav")
    if final_wav.is_file() and final_wav != output_path:
        if output_path.suffix == ".wav":
            return final_wav
        # If output_path was not .wav, rename
        final_wav.rename(output_path)
    return output_path


def download_chat_replay(video_id: str, output_dir: Optional[Path] = None) -> Optional[Path]:
    """Download live chat replay JSON for a stream VOD if available."""
    if output_dir is None:
        output_dir = CHAT_DIR
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    target_pattern = output_dir / f"{video_id}.live_chat.json"
    if target_pattern.is_file() and target_pattern.stat().st_size > 0:
        return target_pattern

    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "subtitleslangs": ["live_chat"],
        "outtmpl": str(output_dir / f"{video_id}"),
        "quiet": True,
        "no_warnings": True,
    }
    if os.path.isfile(NODE_PATH):
        ydl_opts["js_runtimes"] = {"node": {"path": NODE_PATH}}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"Chat replay download error for {video_id}: {e}")

    # Check for created live_chat file (.live_chat.json or .live_chat.jsonl)
    for p in output_dir.glob(f"{video_id}*.json*"):
        return p
    return None
