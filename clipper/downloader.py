"""Downloader module with audio-first stream ingestion, section clipping, and quality inspection."""
import json
import logging
import math
import os
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import load_config
from .process_manager import check_cancelled_or_paused, run_tracked_command
from .utils import get_ffmpeg_path, get_twitch_downloader_path

logger = logging.getLogger("clipper.downloader")


def sanitize_filename(name: str) -> str:
    """Sanitize a string for safe use as a filename on Windows."""
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    return clean.replace(" ", "_")[:80]


def is_url(path_or_url: str) -> bool:
    """Check if input is an HTTP(S) URL."""
    return path_or_url.startswith("http://") or path_or_url.startswith("https://")


def is_twitch(url: str) -> bool:
    """Check if URL is a Twitch link."""
    return "twitch.tv" in url.lower()


def is_youtube(url: str) -> bool:
    """Check if URL is a YouTube link."""
    low = url.lower()
    return "youtube.com" in low or "youtu.be" in low


def is_kick(url: str) -> bool:
    """Check if URL is a Kick link."""
    return "kick.com" in url.lower()


def get_vod_id(input_source: str) -> str:
    """Extract a clean, unique identifier for a stream or local file."""
    if not is_url(input_source):
        base = os.path.splitext(os.path.basename(input_source))[0]
        return f"local_{sanitize_filename(base)}"

    if is_twitch(input_source):
        m = re.search(r"videos/(\d+)", input_source)
        if m:
            return f"twitch_{m.group(1)}"
        return f"twitch_{sanitize_filename(input_source)}"

    if is_youtube(input_source):
        m = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", input_source)
        if m:
            return f"yt_{m.group(1)}"
        return f"yt_{sanitize_filename(input_source)}"

    if is_kick(input_source):
        m = re.search(r"video/([0-9a-fA-F-]+)", input_source)
        if m:
            return f"kick_{m.group(1)}"
        return f"kick_{sanitize_filename(input_source)}"

    return f"stream_{sanitize_filename(input_source)}"


def parse_youtube_live_chat(yt_chat_file: str) -> List[Dict[str, Any]]:
    """Parse yt-dlp YouTube live chat JSON / JSONL and convert to standard comments format."""
    if not os.path.isfile(yt_chat_file):
        logger.warning(f"YouTube chat file not found: {yt_chat_file}")
        return []

    comments: List[Dict[str, Any]] = []

    with open(yt_chat_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue

            action = item.get("replayChatItemAction", {})
            sub_actions = action.get("actions", [])
            for sa in sub_actions:
                chat_item = sa.get("addChatItemAction", {}).get("item", {})
                msg_renderer = (
                    chat_item.get("liveChatTextMessageRenderer")
                    or chat_item.get("liveChatPaidMessageRenderer")
                )
                if not msg_renderer:
                    continue

                offset_sec = None
                offset_msec = msg_renderer.get("videoOffsetTimeMsec")
                if offset_msec is not None:
                    try:
                        offset_sec = float(offset_msec) / 1000.0
                    except (ValueError, TypeError):
                        pass

                if offset_sec is None:
                    offset_time = action.get("videoOffsetTimeMsec")
                    if offset_time is not None:
                        try:
                            offset_sec = float(offset_time) / 1000.0
                        except (ValueError, TypeError):
                            pass

                runs = msg_renderer.get("message", {}).get("runs", [])
                text = "".join(r.get("text", "") for r in runs if isinstance(r, dict))

                if offset_sec is not None and text:
                    comments.append({
                        "content_offset_seconds": offset_sec,
                        "message": {"body": text}
                    })

    comments.sort(key=lambda c: c["content_offset_seconds"])
    logger.info(f"Parsed {len(comments)} comments from YouTube live chat.")
    return comments


def get_ytdlp_path() -> str:
    """Find yt-dlp binary in PATH or virtual environment."""
    found = shutil.which("yt-dlp")
    if found:
        return found
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_bin = os.path.join(base, ".venv", "Scripts", "yt-dlp.exe")
    if os.path.isfile(venv_bin):
        return venv_bin
    return "yt-dlp"


def get_available_qualities(input_source: str) -> List[Dict[str, Any]]:
    """Inspect available resolutions and estimated file sizes for a stream URL or file."""
    if not input_source or not input_source.strip():
        return []

    if not is_url(input_source):
        abs_p = os.path.abspath(input_source)
        if os.path.isfile(abs_p):
            sz_mb = os.path.getsize(abs_p) / (1024 * 1024)
            sz_str = f"{sz_mb/1024:.2f} GB" if sz_mb > 1024 else f"{sz_mb:.1f} MB"
            return [{
                "id": "best",
                "label": f"Local Video Source ({sz_str})",
                "height": 1080,
                "size_mb": sz_mb,
            }]
        return []

    ytdlp_bin = get_ytdlp_path()
    try:
        cmd = [ytdlp_bin, "-J", "--no-playlist", input_source]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            duration = data.get("duration") or 0
            formats = data.get("formats", [])

            def calc_size_mb(f: dict) -> float:
                if f.get("filesize"):
                    return f["filesize"] / (1024 * 1024)
                if f.get("filesize_approx"):
                    return f["filesize_approx"] / (1024 * 1024)
                tbr = f.get("tbr") or ((f.get("vbr") or 0) + (f.get("abr") or 0))
                if tbr and duration:
                    return (tbr * 1000 * duration) / (8 * 1024 * 1024)
                return 0.0

            audio_tracks = [
                f for f in formats
                if f.get("acodec") not in ("none", None) and f.get("vcodec") in ("none", None)
            ]
            best_audio = max(audio_tracks, key=calc_size_mb, default=None)
            audio_mb = calc_size_mb(best_audio) if best_audio else 0.0

            def fmt_size(sz_mb: float) -> str:
                if sz_mb >= 1024:
                    return f"~{sz_mb/1024:.2f} GB"
                elif sz_mb > 0:
                    return f"~{sz_mb:.1f} MB"
                return "size unknown"

            video_formats = [f for f in formats if f.get("vcodec") not in ("none", None) and f.get("height")]
            max_height = max((f.get("height", 0) for f in video_formats), default=1080)
            best_video = max(video_formats, key=calc_size_mb, default=None)
            best_mb = (calc_size_mb(best_video) + audio_mb) if best_video else 0.0

            options = []

            # 1. 1080p
            v_1080 = [f for f in video_formats if f.get("height") == 1080]
            if v_1080:
                mb_1080 = max(calc_size_mb(f) for f in v_1080) + audio_mb
                sz_str = fmt_size(mb_1080) if mb_1080 > 0 else "size unknown"
                options.append({"id": "1080p", "label": f"1080p ({sz_str})", "height": 1080, "size_mb": mb_1080})
            else:
                options.append({"id": "1080p", "label": "1080p (size unknown)", "height": 1080, "size_mb": 0.0})

            # 2. Best available
            best_sz = fmt_size(best_mb) if best_mb > 0 else "size unknown"
            res_str = f"{max_height}p " if max_height else ""
            options.append({
                "id": "best",
                "label": f"Best available ({res_str}{best_sz})",
                "height": max_height,
                "size_mb": best_mb,
            })

            # 3. 720p
            v_720 = [f for f in video_formats if f.get("height") == 720]
            if v_720:
                mb_720 = max(calc_size_mb(f) for f in v_720) + audio_mb
                sz_str = fmt_size(mb_720) if mb_720 > 0 else "size unknown"
                options.append({"id": "720p", "label": f"720p ({sz_str})", "height": 720, "size_mb": mb_720})
            else:
                options.append({"id": "720p", "label": "720p (size unknown)", "height": 720, "size_mb": 0.0})

            # 4. 480p
            v_480 = [f for f in video_formats if f.get("height") == 480]
            if v_480:
                mb_480 = max(calc_size_mb(f) for f in v_480) + audio_mb
                sz_str = fmt_size(mb_480) if mb_480 > 0 else "size unknown"
                options.append({"id": "480p", "label": f"480p ({sz_str})", "height": 480, "size_mb": mb_480})
            else:
                options.append({"id": "480p", "label": "480p (size unknown)", "height": 480, "size_mb": 0.0})

            return options

    except Exception as e:
        logger.warning(f"Failed to query qualities from URL: {e}")

    return [
        {"id": "1080p", "label": "1080p (size unknown)", "height": 1080, "size_mb": 0.0},
        {"id": "best", "label": "Best available (size unknown)", "height": None, "size_mb": 0.0},
        {"id": "720p", "label": "720p (size unknown)", "height": 720, "size_mb": 0.0},
        {"id": "480p", "label": "480p (size unknown)", "height": 480, "size_mb": 0.0},
    ]


def _parse_ytdlp_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse yt-dlp stdout line to extract percentage, speed, total size, and ETA."""
    pct_match = re.search(r"(\d+\.?\d*)%", line)
    speed_match = re.search(r"at\s+([\d\.]+\s*[KkMmGg]i?B/s)", line)
    size_match = re.search(r"of\s+~?\s*([\d\.]+\s*[KkMmGg]i?B)", line)
    eta_match = re.search(r"ETA\s+([\d:]+)", line)
    frag_match = re.search(r"\(frag\s+(\d+)(?:/(\d+))?\)", line)

    data = {}
    if pct_match:
        try:
            data["percent"] = float(pct_match.group(1))
        except ValueError:
            pass
    elif frag_match and frag_match.group(2):
        try:
            curr = float(frag_match.group(1))
            total = float(frag_match.group(2))
            data["percent"] = round((curr / total) * 100, 1)
        except ValueError:
            pass

    if speed_match:
        data["speed"] = speed_match.group(1).strip()
    if size_match:
        data["size"] = size_match.group(1).strip()
    if eta_match:
        data["eta"] = eta_match.group(1).strip()
    if frag_match:
        data["fragment"] = frag_match.group(0)

    return data if data else None


def download_audio_and_chat(
    input_source: str,
    chat_file: Optional[str] = None,
    output_dir: str = "downloads",
    job_id: Optional[str] = None,
    progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Tuple[str, Optional[str], str]:
    """Phase 1: Download AUDIO ONLY first, plus chat.

    Does NOT download full video upfront.
    Returns:
        (audio_path, chat_path, vod_id)
    """
    check_cancelled_or_paused(job_id)
    vod_id = get_vod_id(input_source)
    vod_dir = os.path.abspath(os.path.join(output_dir, vod_id))
    os.makedirs(vod_dir, exist_ok=True)

    audio_dest = os.path.join(vod_dir, "audio.m4a")
    chat_dest = os.path.join(vod_dir, "chat.json")

    # Local file resolution
    if not is_url(input_source):
        local_media = os.path.abspath(input_source)
        if not os.path.isfile(local_media):
            raise FileNotFoundError(f"Local file not found: {local_media}")

        # Check if already audio
        _, ext = os.path.splitext(local_media.lower())
        if ext in (".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg"):
            audio_dest = local_media
        else:
            # Extract audio from local video with ffmpeg
            if not os.path.isfile(audio_dest) or os.path.getsize(audio_dest) < 1024:
                ffmpeg_bin = get_ffmpeg_path()
                logger.info(f"Extracting audio track from local file {local_media} -> {audio_dest}")
                cmd = [
                    ffmpeg_bin, "-y",
                    "-i", local_media,
                    "-vn", "-acodec", "copy",
                    audio_dest
                ]
                ret = run_tracked_command(cmd, job_id=job_id)
                if ret != 0:
                    # Retry with re-encoding to aac
                    cmd = [
                        ffmpeg_bin, "-y",
                        "-i", local_media,
                        "-vn", "-acodec", "aac", "-b:a", "192k",
                        audio_dest
                    ]
                    run_tracked_command(cmd, job_id=job_id)

        # Check local chat
        resolved_chat = None
        if chat_file and os.path.isfile(chat_file):
            resolved_chat = os.path.abspath(chat_file)
        else:
            base_p, _ = os.path.splitext(local_media)
            for cand in [base_p + ".json", base_p + "_chat.json", os.path.join(os.path.dirname(local_media), "chat.json")]:
                if os.path.isfile(cand):
                    resolved_chat = cand
                    break

        if progress_callback:
            progress_callback("download_audio", {"percent": 100.0, "status": "Local audio ready"})
        return audio_dest, resolved_chat, vod_id

    # Twitch Stream / VOD
    if is_twitch(input_source):
        match = re.search(r"videos/(\d+)", input_source)
        twitch_vid_id = match.group(1) if match else input_source.strip()
        td_bin = get_twitch_downloader_path()
        if not td_bin:
            raise RuntimeError("TwitchDownloaderCLI not found in bin/TwitchDownloaderCLI.exe")

        # 1. Download Audio
        if os.path.isfile(audio_dest) and os.path.getsize(audio_dest) > 1024:
            logger.info(f"Using cached Twitch audio: {audio_dest}")
            if progress_callback:
                progress_callback("download_audio", {"percent": 100.0, "status": "Cached audio ready"})
        else:
            logger.info(f"Downloading Twitch audio ({twitch_vid_id}) to {audio_dest}...")
            cmd = [
                td_bin, "videodownload",
                "-u", twitch_vid_id,
                "-o", audio_dest,
                "--collision", "Overwrite"
            ]

            def on_twitch_audio_line(line: str):
                check_cancelled_or_paused(job_id)
                m = re.search(r"(\d+\.?\d*)%", line)
                if m and progress_callback:
                    progress_callback("download_audio", {"percent": float(m.group(1)), "raw": line.strip()})

            ret = run_tracked_command(cmd, job_id=job_id, line_callback=on_twitch_audio_line)
            if ret != 0:
                raise RuntimeError(f"Twitch audio download failed with code {ret}")

        # 2. Download Chat
        if os.path.isfile(chat_dest) and os.path.getsize(chat_dest) > 100:
            logger.info(f"Using cached Twitch chat: {chat_dest}")
        else:
            logger.info(f"Downloading Twitch chat JSON ({twitch_vid_id})...")
            cmd = [
                td_bin, "chatdownload",
                "-u", twitch_vid_id,
                "-o", chat_dest,
                "--collision", "Overwrite"
            ]
            try:
                run_tracked_command(cmd, job_id=job_id)
            except Exception as e:
                logger.warning(f"Could not download Twitch chat: {e}")

        final_chat = chat_dest if os.path.isfile(chat_dest) else None
        return audio_dest, final_chat, vod_id

    # YouTube / Kick
    ytdlp_bin = get_ytdlp_path()

    # 1. Download Audio via yt-dlp
    if os.path.isfile(audio_dest) and os.path.getsize(audio_dest) > 1024:
        logger.info(f"Using cached audio: {audio_dest}")
        if progress_callback:
            progress_callback("download_audio", {"percent": 100.0, "status": "Cached audio ready"})
    else:
        logger.info(f"Downloading audio-only stream ({input_source}) to {audio_dest}...")
        cmd = [
            ytdlp_bin,
            "--continue",
            "--part",
            "--newline",
            "-f", "ba[ext=m4a]/ba/bestaudio",
            "-o", audio_dest,
            input_source
        ]

        def on_ytdlp_audio_line(line: str):
            check_cancelled_or_paused(job_id)
            info = _parse_ytdlp_line(line)
            if info and progress_callback:
                progress_callback("download_audio", info)

        ret = run_tracked_command(cmd, job_id=job_id, line_callback=on_ytdlp_audio_line)
        if ret != 0:
            raise RuntimeError(f"Audio download failed with exit code {ret}")

    # 2. YouTube Chat & Heatmap
    final_chat = None
    if is_youtube(input_source):
        heatmap_dest = os.path.join(vod_dir, "heatmap.json")
        # Try fetching heatmap & info json if missing
        if not os.path.isfile(heatmap_dest):
            try:
                info_cmd = [ytdlp_bin, "-J", "--no-playlist", input_source]
                res = subprocess.run(info_cmd, capture_output=True, text=True, timeout=20)
                if res.returncode == 0 and res.stdout:
                    info_data = json.loads(res.stdout)
                    heatmap = info_data.get("heatmap")
                    if heatmap:
                        with open(heatmap_dest, "w", encoding="utf-8") as hf:
                            json.dump(heatmap, hf, indent=2)
                        logger.info(f"Saved YouTube heatmap data to {heatmap_dest}")
            except Exception as e:
                logger.debug(f"Could not retrieve YouTube heatmap: {e}")

        # Download Chat
        if os.path.isfile(chat_dest) and os.path.getsize(chat_dest) > 100:
            final_chat = chat_dest
        else:
            raw_chat_prefix = os.path.join(vod_dir, "raw_chat")
            chat_cmd = [
                ytdlp_bin,
                "--skip-download",
                "--write-subs",
                "--sub-langs", "live_chat",
                "-o", raw_chat_prefix,
                input_source
            ]
            try:
                run_tracked_command(chat_cmd, job_id=job_id, timeout=60)
            except Exception as e:
                logger.info(f"YouTube chat not available or skipped: {e}")

            raw_file = raw_chat_prefix + ".live_chat.json"
            if os.path.isfile(raw_file):
                comments = parse_youtube_live_chat(raw_file)
                if comments:
                    with open(chat_dest, "w", encoding="utf-8") as cf:
                        json.dump({"comments": comments}, cf, indent=2)
                    final_chat = chat_dest

    return audio_dest, final_chat, vod_id


def format_section_timestamp(seconds: float) -> str:
    """Format seconds into HH:MM:SS.mmm for yt-dlp section download."""
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{mins:02d}:{secs:06.3f}"


def download_video_section(
    input_source: str,
    start_sec: float,
    end_sec: float,
    quality: str = "1080p",
    output_dir: str = "downloads",
    padding_seconds: Optional[float] = None,
    job_id: Optional[str] = None,
) -> Tuple[str, float]:
    """Phase 1: Download ONLY the required video section for a selected moment with padding.

    Returns:
        (section_file_path, moment_start_offset_within_section)
    """
    check_cancelled_or_paused(job_id)
    cfg = load_config()
    pad = padding_seconds if padding_seconds is not None else cfg.get("audio", {}).get("section_padding_seconds", 3.0)

    actual_start = max(0.0, start_sec - pad)
    actual_end = end_sec + pad
    offset = start_sec - actual_start

    vod_id = get_vod_id(input_source)
    sections_dir = os.path.abspath(os.path.join(output_dir, vod_id, "sections"))
    os.makedirs(sections_dir, exist_ok=True)

    sec_filename = f"sec_{int(actual_start)}_{int(math.ceil(actual_end))}_{quality}.mp4"
    section_dest = os.path.join(sections_dir, sec_filename)

    # Cache hit
    if os.path.isfile(section_dest) and os.path.getsize(section_dest) > 1024:
        logger.info(f"Using cached video section: {section_dest}")
        return section_dest, offset

    # Local file cut
    if not is_url(input_source):
        local_video = os.path.abspath(input_source)
        if not os.path.isfile(local_video):
            raise FileNotFoundError(f"Local video file not found: {local_video}")

        ffmpeg_bin = get_ffmpeg_path()
        dur = actual_end - actual_start
        cmd = [
            ffmpeg_bin, "-y",
            "-ss", str(actual_start),
            "-i", local_video,
            "-t", str(dur),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            section_dest
        ]
        ret = run_tracked_command(cmd, job_id=job_id)
        if ret != 0:
            raise RuntimeError(f"Failed to cut local section {section_dest}")
        return section_dest, offset

    # Twitch section download
    if is_twitch(input_source):
        td_bin = get_twitch_downloader_path()
        match = re.search(r"videos/(\d+)", input_source)
        twitch_vid_id = match.group(1) if match else input_source.strip()

        twitch_q = "1080p60" if quality == "1080p" else ("720p60" if quality == "720p" else "480p30")

        cmd = [
            td_bin, "videodownload",
            "-u", twitch_vid_id,
            "-b", f"{int(actual_start)}s",
            "-e", f"{int(math.ceil(actual_end))}s",
            "-q", twitch_q,
            "-o", section_dest,
            "--collision", "Overwrite"
        ]
        ret = run_tracked_command(cmd, job_id=job_id)
        if ret != 0:
            raise RuntimeError(f"Twitch section download failed with code {ret}")
        return section_dest, offset

    # YouTube / Kick section download via yt-dlp
    ytdlp_bin = get_ytdlp_path()
    if quality == "1080p":
        format_selector = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/best"
    elif quality == "720p":
        format_selector = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/best"
    elif quality == "480p":
        format_selector = "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/best"
    else:
        format_selector = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best"

    time_range = f"*{format_section_timestamp(actual_start)}-{format_section_timestamp(actual_end)}"
    cmd = [
        ytdlp_bin,
        "--download-sections", time_range,
        "-f", format_selector,
        "--merge-output-format", "mp4",
        "-o", section_dest,
        input_source
    ]
    ret = run_tracked_command(cmd, job_id=job_id)
    if ret != 0:
        raise RuntimeError(f"yt-dlp section download failed with code {ret}")

    return section_dest, offset


def prepare_input(
    input_source: str,
    chat_file: Optional[str] = None,
    quality: str = "1080p",
    output_dir: str = "downloads",
    job_id: Optional[str] = None,
    progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Tuple[str, Optional[str]]:
    """Backward compatibility wrapper: returns (media_path, chat_path)."""
    audio_path, chat_path, _ = download_audio_and_chat(
        input_source=input_source,
        chat_file=chat_file,
        output_dir=output_dir,
        job_id=job_id,
        progress_callback=progress_callback,
    )
    return audio_path, chat_path
