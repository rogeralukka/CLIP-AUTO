# Stream Clipper 🎬

A 100% free, local stream clipping pipeline optimized for Windows 11 and NVIDIA RTX GPUs. Automatically turns long streamer VODs into captioned vertical (9:16) short clips ranked by chat hype moments.

Zero paid APIs. Zero cloud subscriptions. Runs entirely locally on your HP Omen 16 RTX 5070 Ti.

---

## Folder Structure

```
CLIP AUTO/
├── bin/                       # Local executables (TwitchDownloaderCLI.exe)
├── campaigns/                 # Campaign configurations (YAML)
├── presets/                   # Caption styling & subtitle presets (ASS)
│   ├── classic_yellow.json    # Vibrant yellow karaoke fill, bold outline
│   ├── neon_green.json        # High-visibility neon green fill
│   ├── cyber_cyan.json        # Cyan fill suited for tech & gaming VODs
│   └── clean_white.json       # Minimalist pure white karaoke highlight
├── clipper/                   # Core Python pipeline modules
│   ├── __init__.py
│   ├── captions.py            # Word-by-word karaoke ASS generator & presets
│   ├── cutter.py              # Accurate re-encode cutting & 9:16 NVENC render
│   ├── detection.py           # Chat spike detector & FFmpeg audio loudness fallback
│   ├── downloader.py          # TwitchDownloaderCLI & yt-dlp downloader + cache
│   ├── pipeline.py            # End-to-end pipeline orchestrator
│   └── utils.py               # CUDA/cuBLAS/cuDNN and path detection
├── tests/                     # Unit and integration tests (synthetic test VOD & chat)
├── exports/                   # Organized ready-to-upload exported clips
├── clipper.py                 # Main CLI entry point
├── check_setup.py             # Diagnostic test suite
├── setup_windows.ps1          # One-click Windows setup script
├── requirements.txt           # Python package requirements
└── README.md                  # This guide
```

---

## Quick Setup (Phase 0)

Open PowerShell in this folder and run:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_windows.ps1
```

To verify your environment anytime:
```powershell
.\.venv\Scripts\python.exe check_setup.py
```

---

## CLIP AUTO Studio (new web UI) ⭐

Double-click `start_studio.bat`, or run:
```powershell
.\.venv\Scripts\python.exe server.py
```
Then open http://127.0.0.1:8765 (local only, nothing leaves your laptop).

- **New run** – paste several links at once, pick a campaign, clip count and sensitivity.
- **Queue** – live stage progress, pause / resume / cancel.
- **Review** – the stream's hype curve with every detected moment on it, "why it was picked"
  signal bars, the AI judge's verdict, **listen to each moment's audio before rendering**,
  nudge start/end ±5s, edit the hook title. Keys: `J`/`K` move, `X` select, `P` play.
- **Clips** – phone-sized players, QC badges, switch layout (Split / Gameplay / Face) and
  re-render one clip, Keep / Reject, copy captions per platform, export kept clips + captions.
  Keys: `J`/`K`, `Space`, `Y` keep, `N` reject, `1`/`2`/`3` layout.
- **Live** – watch a live stream (reads Twitch chat too) and get clips minutes after the moment.
- **Campaigns** – rules, hashtags, disclosure, length limits, overlay image.
- **Settings** – AI judge on/off + model, Whisper model, progress bar, gameplay zoom.

The old Streamlit UI (`app.py`) still works as a fallback.

---

## Running the Web Application (Streamlit UI, legacy)

To launch the web interface:
```powershell
.\.venv\Scripts\streamlit.exe run app.py
```
Or with python:
```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Features included in the web interface:
- **Clean Single-Screen Workflow**: Enter VOD link, select campaign, choose detected quality, and start.
- **Campaign Management**: Add, edit, and delete campaign profiles (streamer, disclosure, hashtags, length limits, rules, and risk-word filtering).
- **Background Queue & Progress**: Live job tracking with real-time percentage, stage checklists, pause/resume/cancel controls, and isolated per-job logs.
- **Review Before Render**: Review and untick moments before burning captions and rendering 9:16 vertical shorts.
- **Clips Gallery & Batch Export**: Preview clips, edit titles/descriptions, and export kept clips with ready-to-copy metadata.

---

## Running the Pipeline (CLI)

All commands are run using the virtual environment Python: `.\.venv\Scripts\python.exe clipper.py`.

### 1. Test Dry Run (Synthetic Video & Chat)
Verify your entire GPU pipeline end-to-end without needing any internet connection or external video:
```powershell
.\.venv\Scripts\python.exe clipper.py --dry-run
```

### 2. Run on a Real Twitch VOD (Direct Link)
Pass the Twitch VOD URL. It downloads the VOD and chat replay via `TwitchDownloaderCLI`, finds chat spikes, cuts clips accurately, transcribes with Whisper `large-v3`, burns karaoke captions, and exports vertical 1080x1920 shorts:
```powershell
.\.venv\Scripts\python.exe clipper.py "https://www.twitch.tv/videos/1234567890" --top 8
```

### 3. Run on a Real YouTube VOD / Stream
Pass a YouTube URL. `yt-dlp` automatically downloads the stream and parses live chat replay:
```powershell
.\.venv\Scripts\python.exe clipper.py "https://www.youtube.com/watch?v=VIDEO_ID" --top 6 --preset neon_green
```

### 4. Run on a Local Video File (with or without chat JSON)
- **With chat JSON**:
  ```powershell
  .\.venv\Scripts\python.exe clipper.py "C:\path\to\stream.mp4" --chat "C:\path\to\chat.json" --top 8
  ```
- **Without chat JSON (Automatic Audio Loudness Peak Detection)**:
  ```powershell
  .\.venv\Scripts\python.exe clipper.py "C:\path\to\stream.mp4" --top 5
  ```

---

## Command Line Flags & Options

| Flag | Default | Description |
|---|---|---|
| `vod` | *Required* | Local video path, Twitch VOD link, or YouTube URL |
| `--chat` | `None` | Path to chat JSON. If omitted, falls back to audio loudness peaks |
| `--top` | `8` | Number of hype moments to extract and render |
| `--preset` | `classic_yellow` | Caption style (`classic_yellow`, `neon_green`, `cyber_cyan`, `clean_white`) |
| `--before` | `20` | Seconds to include before the peak spike |
| `--after` | `30` | Seconds to include after the peak spike |
| `--window` | `5` | Chat spike rolling window size in seconds |
| `--z-thresh` | `2.5` | Z-score threshold for hype detection (lower = more sensitive) |
| `--min-msgs` | `12` | Minimum chat messages in window required to trigger a spike |
| `--model` | `large-v3` | Whisper model (`large-v3`, `medium`, `small`, `base`) |
| `--cpu` | `False` | Force CPU mode (`int8` quantization + `libx264` encoder) |
| `--output-dir`| `clips` | Destination folder for finished vertical clips |
| `--dry-run` | `False` | Execute synthetic test pipeline end-to-end |

---

## How It Works Under The Hood

1. **Intelligent Caching**: Downloaded VODs, chats, rough cuts, and Whisper transcripts are cached in `downloads/` and `clips/`. Re-running with different caption presets or clip counts takes seconds instead of re-downloading or re-transcribing.
2. **Accurate Frame Cuts**: Re-encodes sub-clips rather than using keyframe copy (`-c copy`), eliminating keyframe audio/video drift.
3. **Hardware NVENC Acceleration**: High-speed 1080x1920 encoding via NVIDIA NVENC (`h264_nvenc`, preset `p5`, constant quality `cq 21`), with automatic fallback to `libx264`.
4. **Windows Path Resilient**: Automatically handles Windows file paths with spaces and colons when generating and burning ASS subtitles.
