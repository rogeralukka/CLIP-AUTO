# Stream Clipper (CLIP-AUTO) - GTA 6 Launch Day Upgrade (CHANGES.md)

This document provides a comprehensive log of all architectural improvements, upgrades, test coverage, and operational guidelines added to `CLIP-AUTO` in preparation for GTA 6 launch day (Nov 19).

---

## 2026-11-18 (night 4): Live mode gets the same cam detection as normal runs

- Live clips were cut with the old bare cam check: no stream-wide cam memory, no layout check, so a green-screen
  streamer (IShowSpeed) came out as a plain full-picture clip.
- Live now learns the stream's cams from the last ~2.5 min of the buffer before the first clip (and again every
  ~10 min while none is known), remembers them in `downloads/live_buffer/<id>/stream_cams.json`, uses the same
  layout choice as normal runs (split / duo / Framed), and gives earlier clips a second look + re-render when a
  later look finds a cam they missed.
- Default capture quality is now 1080p60 -> 720p fallback (a small face at 720p was easier to miss).
- Tests: `tests/test_live_cams.py`.

## 2026-11-18 (night 3b): one login per account (its own Chrome profile) + "Open all sites"

- Each account can be tied to its own Chrome profile (dropdown read from Chrome's profile list). Log in once in that
  profile to the account's YouTube / TikTok / Instagram / Facebook; Studio then opens every site inside it, already
  logged in. Nothing is stored in Studio, and no API keys are needed. "Open it to log in" opens all the sign-in pages.
- Each planned post has "Open all sites + file": opens every platform's upload page of that account in its profile and
  selects the clip in Explorer. Only the platforms' own upload/login URLs can be opened.

## 2026-11-18 (night 3): Publish rebuilt - honest statuses, simple screen, secrets protected

Audit of the first Publish & Schedule version (platform rules checked against current docs) and fixes:

- **Default = "Plan" mode.** Studio keeps which clip goes to which account, when, and the caption for each
  platform. At posting time you press Open (YouTube / TikTok Studio / Meta Business Suite), Copy caption,
  Show the file, then use that site's own scheduler - those run in the platform's cloud, so the laptop can be
  off. No logins inside Studio, no API audit, no ban risk. Tick "I posted it" per platform.
- **Auto-upload is now optional (Advanced)** and tells the truth: YouTube (`publishAt`, but an un-audited Google
  project is locked private), Facebook Page Reels (`scheduled_publish_time`), Instagram (API cannot schedule -
  Studio publishes at the time while it runs; real container + `media_publish` flow), TikTok (no schedule field;
  un-audited apps are private-only, so it uploads `SELF_ONLY`).
- **Statuses are real.** Every platform stores PLANNED / UPLOADED_SCHEDULED / HELD / UPLOADED_PRIVATE /
  PUBLISHED / POSTED / FAILED with the error text; the old code marked everything "SCHEDULED" even when all
  uploads failed. A failed post no longer uses up the daily limit or locks the clip to an account.
- Uploads run in the background (the page no longer hangs); times are time-zone aware; the next free slot looks
  up to 30 days ahead; one clip = one account is still enforced.
- **No fake data:** the placeholder "Account A/B/C" with made-up handles is gone; the account list starts empty.
- **Secrets:** tokens and the Google client file are git-ignored (`config/tokens/`, `config/youtube_client_secret.json`,
  `config/social_accounts.yaml`, `config/scheduled_posts.json`); account YAML holds only names/handles. Clip paths
  for publishing must be inside the project's clips folders.
- **New screen:** pick account -> pick clips (thumbnails, drop files from Explorer) -> pick time -> plan list by day
  with one button per platform. Clips page has a single "Add to Publish plan" button. Login help (Google client
  file, Sign in with Google, token boxes) sits under Advanced.
- Tests: `tests/test_social.py`, `tests/test_server_social.py` rewritten (28 tests: statuses, quota/lock release,
  payloads, secrets, endpoints). Not tested against live platforms - try one clip per platform first.

## 2026-11-18 (night 2): AI judge on a model that fits the card + Ollama starts with Studio

- Default judge model is now `qwen3:14b` (~9 GB, fits the 12 GB card entirely; the old 27B spilled
  into system RAM and timed out). Requests turn Qwen3's "thinking" off (it ate the JSON answer),
  use an 8k context, and keep the model loaded between moments (it used to be reloaded for every
  one); the model is unloaded when judging ends so Whisper and the render get the VRAM back.
- Studio starts Ollama in the background (no window) when it opens, and again before judging if
  it is not running. Turn off with `llm.autostart_ollama: false`.

## 2026-11-18 (night): one cam layout for the whole stream + 127 Android emojis

- **Stream-wide cam memory:** an overlay stays put for the whole stream, but detection was run
  clip by clip, so Speed (green screen, leans in and out) was found in some clips and missed in
  others, and clips with only Speed showing fell back to a full-picture clip. Cams found in any clip
  are now remembered (`stream_cams.json` in the job folder) and handed to every other clip: a
  remembered cam comes back when a face really sits in that spot (a stable face, in at least
  35% of the sampled frames). Clips rendered before the cam was first seen get a second look at
  the end of the run and are re-rendered if they were missing one. Cutout people may be bigger
  (up to 34% of the screen height) once a normal cam rectangle proves it is a gaming layout.
- **Emojis:** 127 Android-style emojis (Noto Color Emoji) and ~600 trigger words/phrases ("no way",
  "oh my god", "running", "arrested" ...), up to one a second (max 40 per clip), the same one not
  repeated within 4 s, generic ones limited.

## 2026-11-18 (evening): review page + clips appear as they finish

- **Review:** every moment starts selected (untick what you don't want). Your ticks, start/end
  nudges and hook titles are saved per run in the browser, so switching tabs or refreshing no
  longer resets them. Cleared once you press Render.
- **Render:** each clip is added to Clips the moment it finishes (with its risk check and titles),
  while the rest keep rendering. The Queue card shows "Open N clips so far", and Clips shows a
  "still rendering" line and updates by itself. Marking a clip Kept/Rejected mid-render is kept.

## 2026-11-18 (later still): green-screen streamers, captions on the seams, emojis

- **Green-screen cams (IShowSpeed):** a streamer cut out over the game has no cam border, so the
  panel finder missed them and only the box-cam streamer (Kai) was shown. A second finder now
  looks for a stable face that sits outside every cam panel (game moving around it) and shows a
  head-to-shoulders region as their cam. Two streamers -> the two-cam "duo" layout, both whole.
  This frames the person (game visible behind them); it is not background removal.
- **Captions on the seams:** for cam + game layouts the caption is centred on the border between
  the cam section(s) and the game; for a single picture (Framed, IRL, face) it sits a bit below the
  middle. The seam comes from the same numbers that size the cam band (`plan_cam_band`).
- **Emojis:** small pictures (`assets/emoji/*.png`, from Noto Color Emoji, SIL OFL) laid above the
  caption while matching words are said (haha -> laugh, money -> bag, dead -> skull ...). At most one
  per ~1.6 s, never the same twice in a row, generic ones limited. Pictures, not text, because text
  emoji break on many Windows ffmpeg builds. Plan is saved next to the .ass (`*.emoji.json`).

## 2026-11-18 (later): cams shown whole, not chopped

- Cams were cropped to a narrow slice inside their rectangle and stretched 2-3x to fill the
  band, which looked like 144p. Now the **whole cam picture** is shown, enlarged at most 1.5x
  (`polish.cam_max_upscale`; 2.2x when Real-ESRGAN is installed), centred over a blurred copy
  of itself. The cam band is only as tall as the cam needs, so the game gets the rest (taller,
  closer to native size). Two cams: each 540 wide, whole.
- Green-screen cams (person cut out, no rectangle): head-and-shoulders area around the face,
  generous, never a face-only crop. (Cutting the person out of the background is not done.)
- Real-ESRGAN now kicks in from 1.15x enlargement.

## 2026-11-18: real 1080p downloads, real cam outlines, two-cam layout

Checked on Master's own runs (Kai + Speed co-op stream, PewDiePie Detroit stream, Speed + MrBeast IRL stream).

- **Every clip so far came from a 640x360 file.** The yt-dlp command forced YouTube's
  `android,web` clients, which currently only give the 360p fallback (format 18), and the
  format rule then silently took it. Clients are no longer forced. For each stream the app
  now checks which client offers the sharpest video (default, tv, web_safari, android_vr,
  mweb, ...) and uses that one. Old 360p pieces in the cache are downloaded again, and a clip
  whose source is still under 720p gets a QC warning saying so.
  `start_studio.bat` updates yt-dlp on start (`yt-dlp[default]` includes the JS solver).
- **Cam outline instead of "face box + padding".** A cam is a picture pasted on the game,
  so its border is an edge in every frame while the game keeps changing. The app finds that
  rectangle and crops inside it, so no game leaks into the cam band (the "gameplay twice"
  bug). A rectangle that is part of the room (a poster behind an IRL streamer) is rejected
  because the wall around it doesn't move.
- **Two cams = two-cam layout.** Co-op streams (two streamers' cams) get both cams side by
  side on top (540x768 each) and the game below, with a game crop that leaves both cams out.
  Choosing "Game + facecam" no longer cuts one streamer out.
- **The job's stream type is a preference, checked per clip.** A part of the stream with no
  cam (e.g. the IRL intro) gets Framed instead of a broken split; Follow with two or more
  people on screen gets Framed so nobody is cut out. The reason shows under the clip. Layout
  buttons on a clip are still obeyed as picked (except Split on a two-cam stream).
- **Story games no longer turn a facecam stream into "IRL".** Faces inside the game
  (Detroit: Become Human) used to count as "other people".
- **Follow keeps two people in frame** when they fit in the 9:16 window.
- **Whisper loops** ("I know what it is" x20, "oh oh oh ..."): `condition_on_previous_text`
  off, repetition penalty, stricter compression check, and any word/phrase repeated more
  than 3 times back to back is trimmed to 3 (also in already-saved transcripts). Hooks made of
  one repeated word are skipped.
- **Optional AI sharpening of small cams** (Real-ESRGAN, free, GPU): `setup_upscaler.ps1`
  installs it into `bin/realesrgan`; then cam crops blown up 1.6x or more are AI-upscaled.
  If it isn't installed or fails, clips render as before. Not tested on a GPU yet.

## 1. Summary of Changes Per File

### Core Architecture & Configuration
- **`config/clipper.yaml`** *(New)*: Centralized configuration file containing all tunable numbers with sane defaults:
  - Audio model & VAD settings (`whisper_model: "large-v3"`, `section_padding_seconds: 3.0`)
  - Multi-signal weights (`chat_rate: 0.25`, `chat_keywords: 0.30`, `streamer_loudness: 0.20`, `speech_energy: 0.15`, `youtube_heatmap: 0.10`)
  - Robust z-score parameters (`rolling_window_seconds: 600`, sensitivity thresholds)
  - Facecam auto-layout settings (`presence_threshold: 0.60`, `box_padding: 0.25`, `split_facecam_height: 640`)
  - Polish settings (hook title duration `2.5s`, two-pass loudnorm `-14 LUFS`, silence dead air trimming `0.3s` breath room)
  - Quality check (QC) tolerance thresholds
- **`clipper/config.py`** *(New)*: Module for loading, validating, and retrieving settings from `config/clipper.yaml`.

### Phase 1: Audio-First Ingestion & Full Transcription
- **`clipper/downloader.py`**:
  - Implemented `download_audio_and_chat()` to download audio-only upfront (`TwitchDownloaderCLI videodownload -o <id>.m4a` and `yt-dlp -f "ba[ext=m4a]/ba/bestaudio"`), saving bandwidth and processing time.
  - Added native Kick stream support (`is_kick_url()` and yt-dlp Kick extractor).
  - Implemented `download_video_section()` to fetch only the required video ranges with configurable padding (`section_padding_seconds`), avoiding full video downloads before moment selection.
  - **YouTube Anti-Bot & SABR Streaming Bypass**:
    - Discovered Node.js runtime and registered via `--js-runtimes node:<path>` in `yt-dlp` to execute YouTube n-sig decoders.
    - Added automatic `--extractor-args "youtube:player_client=android,web"` to bypass "Sign in to confirm you're not a bot" challenge on live stream VODs.
    - Updated audio download format to `-f "ba[ext=m4a]/ba/bestaudio/best" -x --audio-format m4a` so it extracts audio cleanly even when YouTube SABR experiment restricts streams to combined format 18.
    - Updated `resume_job()` in `clipper/queue.py` to reset the error state upon retry.
- **`clipper/transcription.py`**:
  - Implemented `transcribe_full_audio()` to transcribe the entire stream audio in a single pass with word timestamps, saving output to `downloads/<vod_id>/transcript.json`.
  - Implemented `slice_transcript()` to extract and offset word timestamps per clip from the full transcript cache.
  - Implemented `unload_whisper_model()` to safely delete faster-whisper model instances, clear CUDA cache, and run garbage collection before any subsequent memory-intensive steps.

### Phase 2: Multi-Signal Hype Curve & Boundaries
- **`presets/hype_keywords.yaml`** *(New)*: Weighted keywords dictionary for hype emotes (POG, W, LMAO), caps bursts, and punctuation walls.
- **`clipper/signals.py`** *(New)*:
  - Robust rolling z-score computation using a 10-minute window with median and Median Absolute Deviation ($1.4826 \times \text{MAD}$).
  - 5-signal composite curve calculation: chat message velocity, weighted hype keywords, streamer audio loudness (via FFmpeg `ebur128`), speech energy (speech rate and exclamations), and YouTube heatmap data.
  - Dynamic weight re-normalization when signals are missing (e.g. Kick streams without chat).
  - Sentence boundary snapping (`snap_moment_boundaries`) to align clip start and end with natural speech pauses while respecting campaign min/max durations.
  - Deduplication engine (`dedupe_moments`) against `downloads/<vod_id>/clipped_moments.json` (banning >50% overlap).
- **`clipper/detection.py`**: Upgraded `find_moments()` to use the composite multi-signal curve with fallback to audio loudness/chat.

### Phase 3: Optional Local LLM Judge via Ollama
- **`clipper/judge.py`** *(New)*:
  - Local LLM candidate ranking via Ollama API (`/api/tags` and `/api/generate`).
  - Evaluates top $3\times N$ hype moments on standalone comprehensibility, payoff presence, and punchiness.
  - Strict JSON schema validation with retry logic: score (0-10), boolean standalone, boolean payoff_inside, hook title (clamped to $\le 7$ words), reason.
  - Blends LLM score with composite hype score based on configurable weights.
  - Strictly enforces the 12GB VRAM constraint: calls `unload_whisper_model()` before inference and passes `keep_alive: 0` to Ollama so model unloads immediately.
  - Graceful silent fallback: if Ollama is not installed or the daemon is offline, preserves hype ranking with zero errors or disruption.

### Phase 4: Facecam-Aware Layouts
- **`models/`** *(New)*: Bundled offline model assets:
  - `blaze_face_short_range.tflite` (229 KB MediaPipe model)
  - `haarcascade_frontalface_default.xml` (930 KB OpenCV Haar cascade)
- **`clipper/facecam.py`** *(New)*:
  - Frame sampling at ~1 frame/second.
  - Dual-backend face detector (`MediaPipe FaceDetector` with automatic fallback to OpenCV `CascadeClassifier`).
  - Face bounding box clustering across sampled frames to locate stable streamer facecam and corner (`top_right`, `top_left`, `bottom_right`, `bottom_left`).
  - Automatic layout selector: returns `"split"` if face presence ratio $\ge 60\%$, else `"gameplay"`. Also supports `"face"` reaction crop.
  - Gameplay crop computation excluding the facecam quadrant to avoid duplicate streamer faces.
- **`clipper/cutter.py`**:
  - Implemented 1080x1920 filter complexes for all three layouts:
    - `"split"`: Facecam cropped and scaled to top ($1080 \times 640$), gameplay cropped to bottom ($1080 \times 1280$) stacked vertically.
    - `"gameplay"`: 16:9 gameplay fit in center ($1080 \times 608$) with darkened, blurred background fill (`boxblur=20:5,eq=brightness=-0.15`).
    - `"face"`: Centered crop on facecam scaled to full 9:16.
- **`clipper/captions.py`**:
  - Added layout awareness to `make_ass()`: adjusts subtitle vertical margin (`MarginV=360` in split mode so captions sit comfortably in the bottom gameplay area, never obstructing the facecam).

### Phase 5: Polish (Hook Titles, Two-Pass Loudnorm, Silence Trimming, QC)
- **`clipper/captions.py`**:
  - Added `extract_hook_phrase()` fallback to grab the first strong punchy phrase from transcript words.
  - Implemented `Style: Hook` in ASS subtitle generation (bold text centered in top safe zone for first 2.5s).
- **`clipper/cutter.py`**:
  - Integrated `get_loudnorm_audio_filter()`: measures audio loudness with FFmpeg `loudnorm` pass 1, applies measured parameters in pass 2 targeting -14 LUFS integrated, -1 dBTP true peak, 11 LRA. Skips recompression if audio is already within target range.
- **`clipper/pipeline.py`**:
  - Integrated silence trimming on clip boundaries: trims leading and trailing dead air (>1s) down to 0.3s breath room without violating campaign minimum length.
- **`clipper/qc.py`** *(New)*:
  - Automated empirical QC check (`perform_qc_check`): validates duration bounds, integrated LUFS ($\pm 2$ LU), black frame sequences via `blackdetect`, frozen frame sequences via `freezedetect`, facecam presence ratio, and copyright music heuristic.
  - Returns structured report with status (`PASS`, `WARN`, `FAIL`). Clips are never deleted; issues are flagged.
  - Platform metadata generator (`generate_platform_metadata`): generates copy-paste ready title, description, and hashtags for YouTube Shorts (<100 chars with `#Shorts`), TikTok (<150 chars), Instagram Reels, and X (<280 chars), including required campaign hashtags and disclosures.
  - Saves `{clip_base}_metadata.json` and `{clip_base}_captions.txt` alongside each rendered clip.

### Phase 6: User Interface (Streamlit)
- **`app.py`**:
  - Multi-link paste: VOD input accepts multiple URLs (one per line); queues an individual job for each link on Start.
  - Review-before-render step: shows timestamp range, composite score breakdown (chat, keywords, loudness, LLM judge), editable hook title, LLM judge notes, and moment selection checkboxes.
  - Clips Gallery enhancements:
    - Editable hook titles that update metadata in real-time.
    - Layout selector dropdown (Split / Gameplay / Face) with a dedicated "Re-render" button that re-processes only that specific clip.
    - QC status badges (`PASS`, `WARN`, `FAIL`) with tooltips displaying measured LUFS, black/frozen frame checks, and warning flags.
    - Expandable platform caption blocks with built-in one-click copy buttons for YouTube Shorts, TikTok, Instagram Reels, and X.
- **`clipper/queue.py`**: Updated `confirm_render_moments()` to persist modified hook titles and moment metadata into the job record.

### Phase 7: Live Mode (Stream Monitoring)
- **`clipper/live.py`** *(New)*:
  - Live stream capture engine (`LiveStreamClipper`) utilizing `streamlink` with fallback to `yt-dlp`.
  - Maintains rolling 15-minute buffer in `downloads/live_buffer/<stream_id>/live_stream.ts`.
  - Runs periodic analysis (every 2-3 minutes), detects composite hype spikes, auto-cuts clips, generates captions with hook titles, renders 9:16 vertical shorts, runs QC checks, and exports to `clips/live/<YYYY-MM-DD>/`.
  - Deduplicates against previously clipped intervals in `clipped_moments.json`.
- **`clipper.py`**: Added `--live <url>` CLI flag to launch live stream monitoring directly.

### Environment & Diagnostics
- **`requirements.txt`**: Added `opencv-python>=4.8`, `mediapipe>=0.10`, and `streamlink>=6.0`.
- **`setup_windows.ps1`**: Added automated verification and downloading of MediaPipe BlazeFace and OpenCV Haar cascade models into `models/`.
- **`check_setup.py`**: Added Check [7/7] verifying OpenCV/MediaPipe face detection runtime and model inference.

---

## 2. What Was Tested

All test suites were executed on the target Windows 11 machine (HP Omen 16, RTX 5070 Ti) and passed 100%:

1. **Phase 0 System Verification (`check_setup.py`)**:
   - `[1/7] FFmpeg`: Gyan.FFmpeg with `--enable-libass` and `--enable-nvenc` verified.
   - `[2/7] NVENC`: Real 2-second 1080p@30fps hardware encode verified (completed in 2.10s).
   - `[3/7] CUDA & NVIDIA Runtime`: RTX 5070 Ti (12GB VRAM, driver 617.42) visible to CTranslate2.
   - `[4/7] faster-whisper large-v3`: Verified inference and word timestamps.
   - `[5/7] yt-dlp`: CLI installation and options verified.
   - `[6/7] TwitchDownloaderCLI`: Version 1.56.5 verified with `videodownload` and `chatdownload`.
   - `[7/7] Facecam Runtime`: OpenCV 5.0.0 and MediaPipe 1.2.3 verified with face inference.
2. **Phase 1 Test Suite (`tests/test_phase1.py`)**: 12 tests passed.
   - Audio-only upfront extraction, Kick URL parsing, targeted section downloads with padding, transcript slicing, and Whisper VRAM cleanup.
3. **Phase 2 Test Suite (`tests/test_phase2.py`)**: 16 tests passed.
   - Rolling robust z-score ($1.4826 \times \text{MAD}$), hype keywords weighting, multi-signal normalization, sentence boundary snapping, and deduplication overlap filtering.
4. **Phase 3 Test Suite (`tests/test_phase3.py`)**: 5 tests passed.
   - LLM JSON schema validation, markdown stripping, 7-word hook clamping, score blending, and silent offline fallback.
5. **Phase 4 Test Suite (`tests/test_phase4.py`)**: 6 tests passed.
   - Face detector initialization, bounding box clustering, box expansion with padding, gameplay crop calculation, and FFmpeg vertical rendering for `gameplay`, `split`, and `face` layouts.
6. **Phase 5 Test Suite (`tests/test_phase5.py`)**: 5 tests passed.
   - Hook title phrase extraction, ASS `Style: Hook` generation, two-pass loudnorm audio filter calculation, platform metadata character constraints, and QC check validation on synthetic clips.
7. **Phase 6 Test Suite (`tests/test_phase6.py`)**: 3 tests passed.
   - Multi-link queueing into separate jobs, review-step hook title persistence, and single-clip layout re-rendering.
8. **Phase 7 Test Suite (`tests/test_phase7.py`)**: 3 tests passed.
   - Streamlink binary discovery, media duration probing, live buffer hype spike detection, vertical clip export, and deduplication against history.
9. **Regression Test Suite (`tests/test_pipeline_and_queue.py`, `tests/test_pause_resume_and_quality.py`)**:
   - Pause, resume, cancel process termination, and quality lookup regressions all passed cleanly.
10. **Pipeline Dry Run (`python clipper.py --dry-run`)**:
    - End-to-end execution on synthetic assets passed without errors.

---

## 3. What Was NOT Tested

1. **Live 6-Hour Stream Overnight Capture**:
   - GTA 6 launch day is scheduled for Nov 19. A continuous multi-hour broadcast on a live streamer was not run overnight; tested with synthetic rolling buffers and streamlink CLI dry-runs.
2. **Live Ollama Inference with Active GPU Model Weights**:
   - Ollama service daemon was not running on the machine during testing. The pipeline was tested against mocked LLM outputs and verified for silent offline fallback (preserving hype score rankings without crashing).
3. **YouTube Authenticated Subscriber-Only Live Chat**:
   - YouTube live chat extraction was tested with standard public chat JSON format; private subscriber-only streams requiring browser session cookies were not tested.

---

## 4. Known Limitations & Edge Cases

1. **Kick Stream Chat Protection**:
   - Kick video and audio streams can be downloaded via yt-dlp. However, Kick chat is protected by Cloudflare bot protection. If chat JSON cannot be fetched, the multi-signal engine automatically re-normalizes weights across available signals (streamer audio loudness + speech energy), ensuring clipping continues without chat.
2. **MediaPipe LiteRT Informational Warning**:
   - When MediaPipe initializes, it emits `Input tensor memory is not aligned according to LiteRt's memory alignment requirements. Reallocating memory and copying data.` This is an internal LiteRT delegate informational message on Windows and does not affect detection accuracy or output quality.
3. **Streamlink Live Buffer Continuity**:
   - If an internet connection drops during live capture, Streamlink attempts reconnection, but un-buffered stream segments during the disconnect cannot be retrieved until the streamer's VOD is published.
4. **VRAM Concurrency**:
   - The 12GB VRAM allocation is respected by unloading faster-whisper before running any LLM step. If the user runs other GPU-intensive tasks (e.g. playing GTA 6 on the laptop simultaneously), use the `--cpu` flag or set `force_cpu: true` to prevent VRAM contention.

---

## 5. Verification Commands for Real VODs

### A. Run CLI on a Twitch VOD (Audio-First + Smart Layouts + QC)
```powershell
.\.venv\Scripts\python.exe clipper.py "https://www.twitch.tv/videos/<VOD_ID>" --top 5 --preset classic_yellow
```

### B. Run CLI on a YouTube VOD
```powershell
.\.venv\Scripts\python.exe clipper.py "https://www.youtube.com/watch?v=<VIDEO_ID>" --top 5 --preset neon_green
```

### C. Run Live Stream Monitoring
```powershell
.\.venv\Scripts\python.exe clipper.py --live "https://www.twitch.tv/<STREAMER_CHANNEL>" --preset classic_yellow
```

### D. Launch the Clean Web Application
```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

---

## Review pass 2 (Claude)

**Bugs fixed**
- Twitch "audio-only" downloaded the full video (TwitchDownloader `videodownload`); now yt-dlp audio-only.
- yt-dlp section downloads now use `--force-keyframes-at-cuts`, so captions don't drift 1-4s.
- Dedupe wrote every *detected* moment to `clipped_moments.json`, so re-running detection on the
  same VOD returned nothing. Moments are now recorded only after they are actually rendered.
- Clip boundaries were centred on the peak (half the clip after the reaction, little setup).
  Chat/loudness peak *after* the cause, so clips now start `before` seconds ahead of the peak and
  end when the hype curve dies down (capped at `after`), then snap to sentences.
- Campaign min/max clip length was never passed into detection; now it is.
- The AI judge only ever saw N candidates (nothing to choose from); it now gets N x multiplier.
- YouTube "most replayed" heatmap was downloaded but never fed into the hype curve; now it is.
- Trailing silence trim cut off laughs/screams after the last word; removed (leading trim kept).
- Re-render with a new layout reused the old .ass (wrong caption position, edited hook ignored);
  new `pipeline.rerender_clip()` rebuilds captions for the new layout/hook.
- `load_config` mutated its defaults (shallow copy); fixed.

**Added**
- Facecam detection scans zoomed tiles so small corner webcams are found; picks the cluster seen
  in the most frames (in-game faces wander); full-screen cams get the Face layout; wider crop.
- Gameplay layout zooms in a bit (configurable 16:9 / 4:3 / 1:1) so it's bigger on phones.
- Progress bar along the bottom of every clip (configurable).
- Live mode: reads Twitch chat anonymously over IRC (no account/key), analyses only the last
  10 minutes each check (it was re-scanning the whole growing buffer), skips moments still
  happening at the live edge, never clips the same moment twice, checks every 30s.
- Hype curve saved per VOD (`downloads/<vod>/hype_curve.json`) for the review UI.
- New web UI: `server.py` (FastAPI) + `web/` (no build step), `start_studio.bat`.
- UI settings are saved to `config/user_overrides.yaml` so `config/clipper.yaml` is never rewritten.

## Real-run fixes (first test on a real YouTube stream)

Problems found on the first real run and what changed:

- **No sound in clips.** Each clip now takes its sound from the full-stream audio that was already downloaded (the same audio Whisper heard and the Review page plays). The section's own sound is only used to line the picture up, so picture, sound and captions stay in sync. QC now **fails** a clip with no sound (it used to count silence as a pass).
- **Fake-looking progress.** Transcription reports real progress ("12:05 of 41:30 of audio done · 35x realtime"). Each step bar fills with its own %, only finished steps are ticked, and the time left is per step, worked out from how fast that step is actually going. Rendering shows live % from ffmpeg. Whisper runs batched on the GPU (much faster).
- **Only 1 moment found.** The combined hype curve is re-scored against its own baseline before picking (averaging the signals shrank every spike below the old threshold). Peaks are picked strongest-first with spacing between them. Auto now looks for about one moment per 5 minutes of stream and always offers a few (weaker ones are marked "maybe"). The YouTube replay graph can no longer decide everything on its own.
- **YouTube chat was always 0.** The chat replay download was killed after 60 seconds; long streams need minutes. It now runs in the background during the audio download and Whisper, and partial chat is used if it is cut short.
- **Clips felt "cut into".** Starts and ends are chosen on words: a clip starts where a sentence starts (after a full stop or a pause) and ends at the end of a sentence after the reaction dies down, with a little room for the laugh. Short audio fades remove clicks.
- **Hook "Yeah".** Filler lines are never used as the hook; if nothing good was said, there is no hook. Long hooks and caption lines wrap to fit the frame.
- **Wrong layout on IRL streams.** Each clip is classed as game + facecam, game only, IRL/camera, or just chatting. A split is only used for a facecam that sits still near an edge. IRL clips get a 9:16 crop that follows the main person. New run has a Stream type picker to force it, and Clips has a "Follow" layout.

## Context, layouts and quality (second real-run feedback)

- **Clips follow the story.** The transcript is split into topics (where the words people use change, plus long pauses). A clip starts where the story around the moment starts and ends where that story is over, not at a fixed number of seconds. Default max length without a campaign is now 90s. With the AI judge on (Ollama), the local model also reads the numbered sentences and picks the first and last sentence a new viewer needs; clips it rates as needing outside context or missing the payoff rank lower.
- **Framed layout (new default for one-picture streams).** The whole 16:9 picture sits in the middle band, on top of a blurred copy of itself filling the screen. Nothing is stretched. Hook goes above the picture, captions below. Follow / Face crops are still one click away per clip.
- **Split layout redone.** 10 bands: top and bottom band hold the hook and captions over a blurred fill; the middle 8 bands are two 1080x768 halves (cam, then gameplay). The gameplay crop keeps away from the cam corner.
- **Quality.** Sections download the best stream up to the chosen height (VP9/AV1 allowed, HDR avoided) and the exact-cut re-encode is near-lossless (was ffmpeg's default CRF 23). The rough cut is near-lossless too. Lanczos scaling, gentle denoise + sharpen (`polish.enhance: light`, set `off` to disable), and high-quality NVENC (VBR CQ 19, spatial/temporal AQ, lookahead) for the final clip.
