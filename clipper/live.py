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
import random
import re
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .captions import extract_hook_phrase, load_preset, make_ass
from .config import load_config
from .cutter import caption_anchor_y, cut_rough_clip, render_vertical_clip
from .detection import find_moments
from .downloader import get_vod_id, get_ytdlp_cmd, is_youtube
from .facecam import detect_facecam, merge_known_cams, resolve_layout
from .qc import generate_platform_metadata, perform_qc_check
from .transcription import transcribe_clip
from .utils import get_ffmpeg_path

logger = logging.getLogger("clipper.live")


class TwitchChatListener(threading.Thread):
    """Anonymous read-only Twitch chat over IRC (no account, no API key).

    Stores (seconds_since_capture_start, message) so live mode can use chat speed and
    "CLIP IT"/"LMAO" spam, which are the strongest live signals.
    """

    def __init__(self, channel: str, t0: float):
        super().__init__(daemon=True, name=f"TwitchChat-{channel}")
        self.channel = channel.lower().lstrip("#")
        self.t0 = t0
        self.messages: List[Tuple[float, str]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
                sock.sendall(b"PASS SCHMOOPIIE\r\n")
                sock.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
                sock.sendall(f"JOIN #{self.channel}\r\n".encode())
                logger.info(f"Live chat connected: #{self.channel}")
                buf = ""
                while not self._stop.is_set():
                    data = sock.recv(4096)
                    if not data:
                        break
                    buf += data.decode("utf-8", errors="ignore")
                    *lines, buf = buf.split("\r\n")
                    for line in lines:
                        if line.startswith("PING"):
                            sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                            continue
                        m = re.search(r"PRIVMSG #\S+ :(.*)$", line)
                        if m:
                            with self._lock:
                                self.messages.append((time.time() - self.t0, m.group(1)))
                sock.close()
            except Exception as e:
                logger.debug(f"Live chat connection error ({e}); reconnecting in 5s")
            self._stop.wait(5)

    def comments_between(self, start: float, end: float) -> List[Dict[str, Any]]:
        """Chat in TwitchDownloader-style dicts, offsets relative to `start`."""
        with self._lock:
            return [{"content_offset_seconds": t - start, "message": {"body": msg}}
                    for t, msg in self.messages if start <= t <= end]

    def stop(self):
        self._stop.set()


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
        quality: str = "1080p60,1080p,720p60,720p,best",
        check_interval_seconds: int = 30,
        buffer_duration_seconds: int = 900,  # 15 minutes rolling buffer
        output_base_dir: str = "clips/live",
        preset_name: str = "classic_yellow",
        campaign: Optional[Dict[str, Any]] = None,
        use_cpu: bool = False,
        sensitivity: float = 2.2,
    ):
        self.stream_url = stream_url
        self.quality = quality
        self.check_interval_seconds = check_interval_seconds
        self.buffer_duration_seconds = buffer_duration_seconds
        self.preset_name = preset_name
        self.campaign = campaign
        self.use_cpu = use_cpu
        self.sensitivity = float(sensitivity)

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
        self._t0 = time.time()
        self.chat: Optional[TwitchChatListener] = None
        self.analysis_window_seconds = 600  # only analyse the last 10 min each check
        self.last_analyzed_time: float = 0.0
        self.live_vod_id = f"live_{self.stream_id}"
        # Same cam memory the normal runs use: an overlay stays put for the whole stream, so a cam
        # found in one clip (or in the first probe) is remembered for every later clip.
        from .pipeline import _load_known_cams
        self.cams_file = os.path.join(self.buffer_dir, "stream_cams.json")
        self.known_cams: List[Dict[str, Any]] = _load_known_cams(self.buffer_dir)
        self.clips_done: List[Dict[str, Any]] = []
        self._checks = 0

    def start_capture_process(self) -> bool:
        """Start background stream capture from the start of the stream using streamlink or yt-dlp."""
        streamlink_bin = get_streamlink_path()

        if streamlink_bin and os.path.isfile(streamlink_bin):
            # Streamlink writes a continuous, single MPEG-TS stream directly to self.buffer_file.
            # It handles YouTube, Twitch, and Kick streams, and ffmpeg can read the .ts buffer
            # incrementally while it is actively being downloaded.
            # --force prevents streamlink from prompting to overwrite if buffer file exists.
            cmd = [
                streamlink_bin,
                "--loglevel", "warning",
                "--force",
                "--hls-live-restart",
                "-o", self.buffer_file,
                self.stream_url,
                self.quality,
            ]
            logger.info(f"Starting live stream capture with streamlink (--hls-live-restart --force): {' '.join(cmd)}")
        else:
            # Fallback to yt-dlp: MUST use single pre-muxed stream format and --no-part so that
            # self.buffer_file is written directly instead of splitting into unjoined .part files.
            cmd = get_ytdlp_cmd(self.stream_url) + [
                "--quiet",
                "--no-part",
                "--live-from-start",
                "-f", "best[height<=1080]/best",
                "-o", self.buffer_file,
                self.stream_url,
            ]
            logger.info(f"Starting live stream capture with yt-dlp fallback (--live-from-start): {' '.join(cmd)}")

        try:
            self._capture_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._t0 = time.time()
            m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)/?$", self.stream_url.strip())
            if m and m.group(1).lower() not in ("videos", "directory"):
                self.chat = TwitchChatListener(m.group(1), self._t0)
                self.chat.start()
            return True
        except Exception as e:
            logger.error(f"Failed to start live stream capture process: {e}")
            return False

    def check_and_clip(self) -> List[Dict[str, Any]]:
        """Inspect the buffer from the start forward, compute hype spikes, cut and render any new moments."""
        if not os.path.isfile(self.buffer_file):
            logger.debug("Buffer file not yet created.")
            return []

        curr_duration = get_media_duration_seconds(self.buffer_file)
        if curr_duration < 30.0:
            logger.info(f"Live buffer warming up: {curr_duration:.1f}s captured (need >= 30s)...")
            return []

        # Start from self.last_analyzed_time (0.0 when stream starts).
        # We step forward through the stream in chunks up to self.analysis_window_seconds.
        overlap = 15.0 if self.last_analyzed_time > 0.0 else 0.0
        start_offset = max(0.0, self.last_analyzed_time - overlap)

        available_dur = curr_duration - start_offset
        if available_dur < 25.0 and self.last_analyzed_time > 0.0:
            logger.debug("Not enough new buffer to analyze yet (need >= 25s).")
            return []

        chunk_len = min(float(self.analysis_window_seconds), available_dur)
        end_offset = start_offset + chunk_len

        tail_wav = os.path.join(self.buffer_dir, "tail.wav")
        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
        subprocess.run([ffmpeg_bin, "-y", "-v", "error",
                        "-ss", f"{start_offset:.2f}",
                        "-t", f"{chunk_len:.2f}",
                        "-i", self.buffer_file,
                        "-vn", "-ac", "1", "-ar", "16000", tail_wav],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.isfile(tail_wav):
            return []

        comments = None
        if self.chat is not None:
            # Chat timestamps are wall-clock since capture start; buffer time ~ the same.
            comments = self.chat.comments_between(start_offset, end_offset) or None

        logger.info(f"Analyzing live buffer from {start_offset:.0f}s to {end_offset:.0f}s ({curr_duration:.0f}s total on disk, "
                    f"{len(comments or [])} chat msgs)...")

        found = find_moments(
            comments=comments,
            video_path=tail_wav,
            z_thresh=self.sensitivity,
            vod_id=None,
            before=20,
            after=20,
            window=5,
            min_clip_length=float((self.campaign or {}).get("min_clip_length") or 15.0),
            max_clip_length=float((self.campaign or {}).get("max_clip_length") or 180.0),
        )

        # Back to buffer time; skip moments still unfolding at the very live edge
        from .signals import dedupe_moments
        new_moments = []
        is_live_edge = (end_offset >= curr_duration - 5.0)
        for m in found:
            m["start"] = round(m["start"] + start_offset, 2)
            m["end"] = round(m["end"] + start_offset, 2)
            if is_live_edge and m["end"] > curr_duration - 3.0:
                continue
            new_moments.append(m)
        new_moments = dedupe_moments(new_moments, vod_id=self.live_vod_id, downloads_dir="downloads")

        self.last_analyzed_time = end_offset

        if not new_moments:
            logger.info("No new hype moments detected in current buffer window.")
            return []

        logger.info(f"Found {len(new_moments)} NEW hype moment(s) in live stream!")
        self._probe_cams(curr_duration)
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

            # Facecam layout: same checks as a normal run (cutout/green-screen cams, two cams,
            # stream-wide cam memory)
            facecam = detect_facecam(rough_path, known_cams=self.known_cams)
            self._remember_cams(facecam)
            layout, layout_note = resolve_layout("auto", facecam)

            # Hook title & ASS
            hook_title = extract_hook_phrase(words)
            make_ass(words, ass_path, preset=preset, layout=layout, hook_title=hook_title,
                     caption_y=caption_anchor_y(layout, facecam))

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
                "layout_note": layout_note,
                "facecam": facecam,
                "qc": qc,
                "platforms": platform_meta,
                # kept so the clip can be re-rendered if a later look finds a cam this one missed
                "index": len(self.clips_done) + 1,
                "rough_path": rough_path,
                "ass_path": ass_path,
                "transcript": transcript_text,
                "preset": self.preset_name,
                "meta_path": meta_file,
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

            from .signals import record_clipped_moment
            record_clipped_moment(self.live_vod_id, start_s, end_s, m.get("score", 0.0))
            logger.info(f"Exported live clip -> {final_path} (QC: {qc['status']})")
            rendered_clips.append(clip_meta)
            self.clips_done.append(clip_meta)

        self._second_look()
        return rendered_clips

    def _remember_cams(self, facecam: Dict[str, Any]) -> None:
        import json as _json
        self.known_cams = merge_known_cams(self.known_cams, (facecam or {}).get("cams") or [])
        try:
            with open(self.cams_file, "w", encoding="utf-8") as f:
                _json.dump(self.known_cams, f)
        except OSError:
            pass

    def _probe_cams(self, curr_duration: float) -> None:
        """Look at the last couple of minutes of the buffer to learn where this stream's cams are,
        so even the FIRST clip knows. Repeats now and then while no cam is known yet."""
        self._checks += 1
        if self.known_cams and self._checks % 20 != 1:
            return
        try:
            span = min(150.0, curr_duration)
            info = detect_facecam(self.buffer_file, start_sec=max(0.0, curr_duration - span),
                                  end_sec=curr_duration, sample_interval=4.0, known_cams=self.known_cams)
            self._remember_cams(info)
            logger.info(f"Cam probe on live buffer: {len(self.known_cams)} cam(s) known for this stream.")
        except Exception as e:  # a failed probe must never stop clipping
            logger.warning(f"Live cam probe failed: {e}")

    def _second_look(self) -> None:
        """Clips made before a cam was known get another look and a re-render if one was missed."""
        import json as _json
        from .pipeline import rerender_clip
        for i, c in enumerate(list(self.clips_done)):
            try:
                fc = c.get("facecam") or {}
                if len(fc.get("cams") or []) >= len(self.known_cams):
                    continue
                if not os.path.isfile(c.get("rough_path", "")):
                    continue
                new_info = detect_facecam(c["rough_path"], known_cams=self.known_cams)
                if len(new_info.get("cams") or []) <= len(fc.get("cams") or []):
                    continue
                logger.info(f"Live clip {c['filename']}: found a cam the first look missed, re-rendering")
                new_clip = rerender_clip(dict(c, facecam=new_info, layout="auto", start_seconds=c["start_seconds"], end_seconds=c["end_seconds"]),
                                         preset_name=self.preset_name, campaign=self.campaign, cpu=self.use_cpu)
                self.clips_done[i] = new_clip
                with open(new_clip["meta_path"], "w", encoding="utf-8") as f:
                    _json.dump(new_clip, f, indent=2, default=str)
            except Exception as e:
                logger.warning(f"Second look at live clip {c.get('filename')} failed: {e}")

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
                # If we have a backlog of buffer from earlier in the stream, sleep briefly
                # so we catch up quickly to the live edge.
                curr_duration = get_media_duration_seconds(self.buffer_file)
                backlog = curr_duration - self.last_analyzed_time
                if backlog > self.analysis_window_seconds * 1.2:
                    time.sleep(2)
                else:
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
        if self.chat is not None:
            self.chat.stop()
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
    interval: int = 30,
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
