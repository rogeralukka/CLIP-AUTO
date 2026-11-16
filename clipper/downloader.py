"""Downloader module with quality inspection, live progress parsing, and process cancellation tracking."""
import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

from .process_manager import check_cancelled_or_paused, run_tracked_command
from .utils import get_twitch_downloader_path

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
    """Inspect available resolutions and estimated file sizes for a stream URL or file.

    Returns a list of options:
    [
      {"id": "1080p", "label": "1080p (~5.50 GB)", "height": 1080, "size_mb": 5500.0},
      {"id": "best", "label": "Best available (1440p ~8.20 GB)", "height": 1440, "size_mb": 8200.0},
      ...
    ]
    """
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

            # Find best audio track
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

            # Available video heights
            video_formats = [f for f in formats if f.get("vcodec") not in ("none", None) and f.get("height")]
            max_height = max((f.get("height", 0) for f in video_formats), default=1080)
            best_video = max(video_formats, key=calc_size_mb, default=None)
            best_mb = (calc_size_mb(best_video) + audio_mb) if best_video else 0.0

            options = []

            # 1. 1080p (Default target)
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

    # Fallback options with no fake sizes if query fails or is offline
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


def download_twitch(
    url_or_id: str,
    output_dir: str = "downloads",
    quality: str = "1080p",
    job_id: Optional[str] = None,
    progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Tuple[str, Optional[str]]:
    """Download Twitch VOD and chat JSON with live progress and partial resumption support."""
    check_cancelled_or_paused(job_id)
    os.makedirs(output_dir, exist_ok=True)
    td_bin = get_twitch_downloader_path()
    if not td_bin:
        raise RuntimeError("TwitchDownloaderCLI not found! Ensure it is in 'bin/TwitchDownloaderCLI.exe'.")

    match = re.search(r"videos/(\d+)", url_or_id)
    vod_id = match.group(1) if match else url_or_id.strip()

    ext = ".m4a" if quality == "audio_only" else ".mp4"
    video_dest = os.path.abspath(os.path.join(output_dir, f"twitch_{vod_id}_{quality}{ext}"))
    chat_dest = os.path.abspath(os.path.join(output_dir, f"twitch_{vod_id}_chat.json"))

    # Map quality string to Twitch quality specifier
    twitch_q = None
    if quality == "1080p":
        twitch_q = "1080p60"
    elif quality == "720p":
        twitch_q = "720p60"
    elif quality == "480p":
        twitch_q = "480p30"
    elif quality == "audio_only":
        twitch_q = "Audio Only"

    # 1. Download Video
    if os.path.isfile(video_dest) and os.path.getsize(video_dest) > 1024 * 1024:
        logger.info(f"Using cached Twitch VOD: {video_dest}")
        if progress_callback:
            progress_callback("download_video", {"percent": 100.0, "status": "Cached VOD loaded"})
    else:
        logger.info(f"Downloading Twitch VOD ({vod_id}, quality={quality}) with TwitchDownloaderCLI...")
        cmd = [
            td_bin, "videodownload",
            "-u", vod_id,
            "-o", video_dest,
            "--collision", "Overwrite"
        ]
        if twitch_q:
            cmd.extend(["-q", twitch_q])

        def on_twitch_line(line: str):
            check_cancelled_or_paused(job_id)
            m = re.search(r"(\d+\.?\d*)%", line)
            if m and progress_callback:
                pct = float(m.group(1))
                progress_callback("download_video", {"percent": pct, "raw": line.strip()})

        ret = run_tracked_command(cmd, job_id=job_id, line_callback=on_twitch_line)
        if ret != 0:
            raise RuntimeError(f"Twitch video download exited with code {ret}")

    check_cancelled_or_paused(job_id)

    # 2. Download Chat JSON
    if os.path.isfile(chat_dest) and os.path.getsize(chat_dest) > 100:
        logger.info(f"Using cached Twitch chat: {chat_dest}")
        if progress_callback:
            progress_callback("download_chat", {"percent": 100.0, "status": "Cached chat loaded"})
    else:
        logger.info(f"Downloading Twitch chat JSON ({vod_id})...")
        if progress_callback:
            progress_callback("download_chat", {"percent": 10.0, "status": "Fetching chat replay..."})

        cmd = [
            td_bin, "chatdownload",
            "-u", vod_id,
            "-o", chat_dest,
            "--collision", "Overwrite"
        ]

        def on_chat_line(line: str):
            check_cancelled_or_paused(job_id)
            m = re.search(r"(\d+\.?\d*)%", line)
            if m and progress_callback:
                pct = float(m.group(1))
                progress_callback("download_chat", {"percent": pct, "raw": line.strip()})

        try:
            run_tracked_command(cmd, job_id=job_id, line_callback=on_chat_line)
        except Exception as e:
            logger.warning(f"Failed to download Twitch chat (might not exist): {e}")
            if not os.path.isfile(chat_dest):
                chat_dest = None

    return video_dest, chat_dest if (chat_dest and os.path.isfile(chat_dest)) else None


def download_youtube(
    url: str,
    output_dir: str = "downloads",
    quality: str = "1080p",
    job_id: Optional[str] = None,
    progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Tuple[str, Optional[str]]:
    """Download YouTube video and live chat using yt-dlp with live progress parsing and partial resumption."""
    check_cancelled_or_paused(job_id)
    os.makedirs(output_dir, exist_ok=True)
    ytdlp_bin = get_ytdlp_path()

    vid_id = sanitize_filename(url)
    ext = ".m4a" if quality == "audio_only" else ".mp4"
    video_dest = os.path.abspath(os.path.join(output_dir, f"yt_{vid_id}_{quality}{ext}"))
    raw_chat_prefix = os.path.abspath(os.path.join(output_dir, f"yt_{vid_id}"))
    converted_chat_dest = os.path.abspath(os.path.join(output_dir, f"yt_{vid_id}_chat.json"))

    # Map quality to yt-dlp format selector (Requirement 2)
    if quality == "1080p":
        format_selector = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/best"
    elif quality == "720p":
        format_selector = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/best"
    elif quality == "480p":
        format_selector = "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/best"
    elif quality == "audio_only":
        format_selector = "ba[ext=m4a]/ba/bestaudio"
    else:  # "best"
        format_selector = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best"

    # 1. Download Video (with --continue for partial resumption)
    if os.path.isfile(video_dest) and os.path.getsize(video_dest) > 1024 * 1024:
        logger.info(f"Using cached YouTube video: {video_dest}")
        if progress_callback:
            progress_callback("download_video", {"percent": 100.0, "status": "Cached video loaded"})
    else:
        logger.info(f"Downloading YouTube video ({vid_id}, quality={quality}) with yt-dlp...")
        cmd = [
            ytdlp_bin,
            "--continue",  # Resume partial downloads (Requirement 1)
            "--part",      # Download into .part file until finished
            "--newline",
            "-f", format_selector,
            "-o", video_dest,
            url
        ]
        if quality != "audio_only":
            cmd.extend(["--merge-output-format", "mp4"])

        def on_ytdlp_line(line: str):
            check_cancelled_or_paused(job_id)
            info = _parse_ytdlp_line(line)
            if info and progress_callback:
                progress_callback("download_video", info)

        ret = run_tracked_command(cmd, job_id=job_id, line_callback=on_ytdlp_line)
        if ret != 0:
            raise RuntimeError(f"yt-dlp video download exited with code {ret}")

    check_cancelled_or_paused(job_id)

    # 2. Download Live Chat
    if os.path.isfile(converted_chat_dest):
        logger.info(f"Using cached converted YouTube chat: {converted_chat_dest}")
        if progress_callback:
            progress_callback("download_chat", {"percent": 100.0, "status": "Cached chat loaded"})
        return video_dest, converted_chat_dest

    logger.info("Attempting to download YouTube live chat...")
    if progress_callback:
        progress_callback("download_chat", {"percent": 20.0, "status": "Checking for live chat replay..."})

    chat_cmd = [
        ytdlp_bin,
        "--skip-download",
        "--write-subs",
        "--sub-langs", "live_chat",
        "-o", raw_chat_prefix,
        url
    ]
    try:
        run_tracked_command(chat_cmd, job_id=job_id, timeout=60)
    except Exception as e:
        logger.info(f"Live chat download skipped or unavailable for {url}: {e}")

    raw_chat_file = raw_chat_prefix + ".live_chat.json"
    if os.path.isfile(raw_chat_file):
        comments = parse_youtube_live_chat(raw_chat_file)
        if comments:
            with open(converted_chat_dest, "w", encoding="utf-8") as f:
                json.dump({"comments": comments}, f, indent=2)
            logger.info(f"Converted YouTube chat saved to: {converted_chat_dest}")
            if progress_callback:
                progress_callback("download_chat", {"percent": 100.0, "status": f"{len(comments)} chat messages loaded"})
            return video_dest, converted_chat_dest

    return video_dest, None


def prepare_input(
    input_source: str,
    chat_file: Optional[str] = None,
    quality: str = "1080p",
    output_dir: str = "downloads",
    job_id: Optional[str] = None,
    progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Tuple[str, Optional[str]]:
    """Resolve input: local file or URL (Twitch/YouTube) with specified quality."""
    check_cancelled_or_paused(job_id)
    if is_url(input_source):
        if is_twitch(input_source):
            return download_twitch(
                input_source, output_dir=output_dir, quality=quality, job_id=job_id, progress_callback=progress_callback
            )
        else:
            return download_youtube(
                input_source, output_dir=output_dir, quality=quality, job_id=job_id, progress_callback=progress_callback
            )

    local_video = os.path.abspath(input_source)
    if not os.path.isfile(local_video):
        raise FileNotFoundError(f"Local video file not found: {local_video}")

    local_chat = None
    if chat_file and os.path.isfile(chat_file):
        local_chat = os.path.abspath(chat_file)
    else:
        base, _ = os.path.splitext(local_video)
        for cand in [base + ".json", base + "_chat.json"]:
            if os.path.isfile(cand):
                local_chat = cand
                break

    if progress_callback:
        progress_callback("download_video", {"percent": 100.0, "status": "Local video loaded"})

    return local_video, local_chat
