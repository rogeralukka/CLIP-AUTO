"""clipper/live.py - Real-time live stream monitoring and rolling buffer clipping.

Captures live streams (Twitch, Kick, YouTube) via streamlink or yt-dlp, computes composite
hype curve periodically over the rolling window, auto-cuts clips on detected spikes,
and exports fully captioned 9:16 vertical shorts to clips/live/<YYYY-MM-DD>/.
"""
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .captions import extract_hook_phrase, load_preset, make_ass
from .config import load_config
from .cutter import cut_rough_clip, render_vertical_clip
from .detection import find_moments
from .downloader import get_vod_id
from .facecam import detect_facecam
from .qc import generate_platform_metadata, perform_qc_check
from .transcription import transcribe_clip
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.live")


def get_streamlink_path() -> Optional[str]:
    """Locate streamlink executable in current python environment or PATH."""
    venv_bin = os.path.join(os.path.dirname(sys.executable), "streamlink.exe")
    if os.path.isfile(venv_bin):
        return venv_bin
    sh = shutil.which("streamlink")
    return sh


def get_media_duration_seconds(media_path: str) -> float:
    """Get accurate media duration in seconds via FFmpeg/ffprobe."""
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    cmd = [ffmpeg_bin, "-i", os.path.abspath(media_path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        # Search Duration: 00:01:23.45
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr)
        if m:
            h, mins, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600.0 + mins * 60.0 + s
    except Exception:
        pass
    return 0.0


class LiveStreamClipper:
    """Live stream capture and real-time clipping engine."""

    def __init__(
        self,
        stream_url: str,
        quality: str = "720p,best",
        check_interval_seconds: int = 120,
        buffer_duration_seconds: int = 900,  # 15 minutes rolling buffer
        output_base_dir: str = "clips/live",
        preset_name: str = "classic_yellow",
        campaign: Optional[Dict[str, Any]] = None,
        use_cpu: bool = False,
    ):
        self.stream_url = stream_url
        self.quality = quality
        self.check_interval_seconds = check_interval_seconds
        self.buffer_duration_seconds = buffer_duration_seconds
        self.preset_name = preset_name
        self.campaign = campaign
        self.use_cpu = use_cpu

        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        self.output_dir = os.path.abspath(os.path.join(output_base_dir, today_str))
        os.makedirs(self.output_dir, exist_ok=True)

        self.stream_id = get_vod_id(stream_url)
        self.buffer_dir = os.path.abspath(os.path.join("downloads", "live_buffer", self.stream_id))
        os.makedirs(self.buffer_dir, exist_ok=True)
        self.buffer_file = os.path.join(self.buffer_dir, "live_stream.ts")
        self.history_file = os.path.join(self.buffer_dir, "clipped_moments.json")

        self._capture_proc: Optional[subprocess.Popen] = None
        self._running = False

    def start_capture_process(self) -> bool:
        """Start background stream capture using streamlink or yt-dlp."""
        streamlink_bin = get_streamlink_path()

        if streamlink_bin and os.path.isfile(streamlink_bin):
            cmd = [
                streamlink_bin,
                "--loglevel", "warning",
                "-o", self.buffer_file,
                self.stream_url,
                self.quality,
            ]
            logger.info(f"Starting live stream capture with streamlink: {' '.join(cmd)}")
        else:
            # Fallback to yt-dlp
            venv_ytdlp = os.path.join(os.path.dirname(sys.executable), "yt-dlp.exe")
            ytdlp_bin = venv_ytdlp if os.path.isfile(venv_ytdlp) else "yt-dlp"
            cmd = [
                ytdlp_bin,
                "--quiet",
                "-f", "bestvideo[height<=1080]+bestaudio/best",
                "-o", self.buffer_file,
                self.stream_url,
            ]
            logger.info(f"Starting live stream capture with yt-dlp: {' '.join(cmd)}")

        try:
            self._capture_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to start live stream capture process: {e}")
            return False

    def check_and_clip(self) -> List[Dict[str, Any]]:
        """Inspect the buffer, compute hype spikes, cut and render any new moments."""
        if not os.path.isfile(self.buffer_file):
            logger.debug("Buffer file not yet created.")
            return []

        curr_duration = get_media_duration_seconds(self.buffer_file)
        if curr_duration < 30.0:
            logger.info(f"Live buffer warming up: {curr_duration:.1f}s captured (need >= 30s)...")
            return []

        logger.info(f"Analyzing live buffer ({curr_duration:.1f}s) for hype spikes...")

        new_moments = find_moments(
            video_path=self.buffer_file,
            z_thresh=2.0,
            vod_id=f"live_{self.stream_id}",
            before=15,
            after=25,
            window=5,
        )

        if not new_moments:
            logger.info("No new hype moments detected in current buffer window.")
            return []

        logger.info(f"Found {len(new_moments)} NEW hype moment(s) in live stream!")
        rendered_clips = []
        preset = load_preset(self.preset_name)

        for m in new_moments:
            start_s = m["start"]
            end_s = m["end"]
            dur = end_s - start_s
            ts_str = datetime.datetime.now().strftime("%H%M%S")
            clip_base = f"live_{ts_str}_{int(start_s)}s"

            rough_path = os.path.join(self.output_dir, f"rough_{clip_base}.mp4")
            ass_path = os.path.join(self.output_dir, f"cap_{clip_base}.ass")
            final_path = os.path.join(self.output_dir, f"{clip_base}.mp4")

            # Cut rough clip
            cut_rough_clip(self.buffer_file, start_s, end_s, rough_path, use_cpu=self.use_cpu)

            # Transcribe
            words = transcribe_clip(rough_path, use_cpu=self.use_cpu, cache_dir=self.output_dir)

            # Facecam layout
            facecam = detect_facecam(rough_path)
            layout = "split" if facecam.get("found") else "gameplay"

            # Hook title & ASS
            hook_title = extract_hook_phrase(words) or f"Live Clip {ts_str}"
            make_ass(words, ass_path, preset=preset, layout=layout, hook_title=hook_title)

            # Render vertical 9:16
            render_vertical_clip(
                rough_path,
                ass_path,
                final_path,
                use_cpu=self.use_cpu,
                layout=layout,
                facecam_info=facecam,
            )

            # QC and metadata
            transcript_text = " ".join(w[2] for w in words)
            qc = perform_qc_check(
                final_path,
                duration=dur,
                campaign=self.campaign,
                facecam_info=facecam,
                word_count=len(words),
                layout=layout,
            )
            platform_meta = generate_platform_metadata(
                hook_title=hook_title,
                transcript=transcript_text,
                campaign=self.campaign,
            )

            meta_file = os.path.join(self.output_dir, f"{clip_base}_metadata.json")
            captions_file = os.path.join(self.output_dir, f"{clip_base}_captions.txt")

            clip_meta = {
                "timestamp": ts_str,
                "filename": os.path.basename(final_path),
                "path": final_path,
                "start_seconds": start_s,
                "end_seconds": end_s,
                "duration": round(dur, 2),
                "hype_score": m.get("score", 0.0),
                "hook_title": hook_title,
                "layout": layout,
                "facecam": facecam,
                "qc": qc,
                "platforms": platform_meta,
            }

            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(clip_meta, f, indent=2)

            with open(captions_file, "w", encoding="utf-8") as f:
                f.write(
                    f"=== {hook_title} ===\n\n"
                    f"--- YOUTUBE SHORTS ---\nTitle: {platform_meta['youtube_title']}\n\n{platform_meta['youtube_description']}\n\n"
                    f"--- TIKTOK ---\n{platform_meta['tiktok_caption']}\n\n"
                    f"--- INSTAGRAM REELS ---\n{platform_meta['reels_caption']}\n\n"
                    f"--- X (TWITTER) ---\n{platform_meta['x_tweet']}\n"
                )

            logger.info(f"Exported live clip -> {final_path} (QC: {qc['status']})")
            rendered_clips.append(clip_meta)

        return rendered_clips

    def run_loop(self, max_iterations: Optional[int] = None):
        """Run the live monitoring loop until interrupted."""
        logger.info(f"Starting Live Stream Monitor for: {self.stream_url}")
        logger.info(f"Clips will be saved to: {self.output_dir}")

        if not self.start_capture_process():
            logger.error("Could not start live stream capture. Exiting.")
            return

        self._running = True
        iterations = 0

        try:
            while self._running:
                time.sleep(self.check_interval_seconds)
                iterations += 1

                try:
                    self.check_and_clip()
                except Exception as e:
                    logger.warning(f"Error during live clipping check: {e}")

                if max_iterations is not None and iterations >= max_iterations:
                    logger.info(f"Reached max iterations ({max_iterations}). Stopping.")
                    break
        except KeyboardInterrupt:
            logger.info("Live monitor stopped by user.")
        finally:
            self.stop()

    def stop(self):
        """Stop background capture and clean up."""
        self._running = False
        if self._capture_proc and self._capture_proc.poll() is None:
            logger.info("Stopping stream capture process...")
            self._capture_proc.terminate()
            try:
                self._capture_proc.wait(timeout=5)
            except Exception:
                self._capture_proc.kill()


def run_live_clipping(
    stream_url: str,
    quality: str = "720p,best",
    interval: int = 120,
    preset: str = "classic_yellow",
    use_cpu: bool = False,
    max_iterations: Optional[int] = None,
):
    """Entry point for CLI `python clipper.py --live <url>`."""
    clipper = LiveStreamClipper(
        stream_url=stream_url,
        quality=quality,
        check_interval_seconds=interval,
        preset_name=preset,
        use_cpu=use_cpu,
    )
    clipper.run_loop(max_iterations=max_iterations)
