"""Generate synthetic test video and fake chat data for offline testing and dry runs."""
import json
import os
import subprocess
from typing import Tuple

from clipper.utils import get_ffmpeg_path


def generate_synthetic_assets(output_dir: str = "tests/test_data") -> Tuple[str, str]:
    """Create a 60-second test video (1920x1080) and simulated Twitch chat JSON.

    The chat contains two intentional hype bursts:
    - Burst 1 at t=20s to t=25s (~30 messages: 'POGGERS', 'INSANE CLIP', 'CLIP IT')
    - Burst 2 at t=42s to t=46s (~35 messages: 'OMEGALUL', 'HOW DID HE DO THAT', 'W STREAMER')
    The audio contains corresponding volume increases at those exact timestamps.
    """
    os.makedirs(output_dir, exist_ok=True)
    video_path = os.path.abspath(os.path.join(output_dir, "synthetic_vod.mp4"))
    chat_path = os.path.abspath(os.path.join(output_dir, "synthetic_chat.json"))

    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"

    # 1. Generate 60-second test video with sine wave audio that peaks at t=22s and t=44s
    if not (os.path.isfile(video_path) and os.path.getsize(video_path) > 10240):
        # We synthesize video with drawtext timecode and audio
        # Volume profile: baseline low, loud at 20-25s and 42-46s
        filter_complex = (
            "testsrc=duration=60:size=1920x1080:rate=30[v];"
            "sine=frequency=440:duration=60,volume='if(between(t,20,25)+between(t,42,46),1.5,0.1)':eval=frame[a]"
        )
        cmd = [
            ffmpeg_bin, "-y",
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k",
            video_path
        ]
        subprocess.run(cmd, capture_output=True, check=True)

    # 2. Generate synthetic chat JSON
    comments = []
    # Baseline quiet chat: 1 message every 3 seconds
    for sec in range(0, 60, 3):
        comments.append({
            "content_offset_seconds": float(sec),
            "message": {"body": f"casual chat message at {sec}s"}
        })

    # Hype Burst 1 (second 20 to 24)
    hype_words_1 = ["POG", "POGGERS", "NO WAY", "INSANE", "WTF", "CLIP THAT", "LOL", "OMG"]
    for i in range(28):
        offset = 20.0 + (i * 0.15)
        comments.append({
            "content_offset_seconds": round(offset, 2),
            "message": {"body": hype_words_1[i % len(hype_words_1)]}
        })

    # Hype Burst 2 (second 42 to 46)
    hype_words_2 = ["OMEGALUL", "LMAOOOO", "DEAD", "W PLAY", "SHEEEESH", "GOLD"]
    for i in range(32):
        offset = 42.0 + (i * 0.12)
        comments.append({
            "content_offset_seconds": round(offset, 2),
            "message": {"body": hype_words_2[i % len(hype_words_2)]}
        })

    comments.sort(key=lambda c: c["content_offset_seconds"])

    with open(chat_path, "w", encoding="utf-8") as f:
        json.dump({"comments": comments}, f, indent=2)

    return video_path, chat_path


if __name__ == "__main__":
    v, c = generate_synthetic_assets()
    print(f"Generated synthetic test assets:\nVideo: {v}\nChat : {c}")
