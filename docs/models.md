# Model License and Benchmark Ledger

This document maintains the canonical inventory of all machine learning models planned and used in CLIP AUTO's new engine ("Hear, See, Understand, Then Clip").

In accordance with **Rule R1** and **Rule R3**, all licenses are verified directly against official repositories and model cards. Performance metrics (VRAM and Speed) are measured empirically on the local target hardware (**NVIDIA GeForce RTX 5070 Ti Laptop GPU 12 GB, Windows 11**) during Milestone M0 benchmark passes.

---

## 1. Model Inventory & Licensing Table

| Model Slot | Model Name | Exact Hugging Face / GitHub ID | Size | Official License | Commercial Use OK? | Measured Peak VRAM | Measured Speed (30s Audio/Video) |
|---|---|---|---|---|---|---|---|
| **H1: Stem Separation** | Hybrid Transformer Demucs | `facebookresearch/demucs` (`htdemucs`) | ~80 MB | MIT | Yes | **804.0 MB** reserved (608.2 MB alloc) | **1.85s** (16.3x real-time) |
| **H1: Voice Activity** | Silero VAD v5 | `snakers4/silero-vad` | ~2 MB | MIT | Yes | **56.0 MB** reserved (35.9 MB alloc) | **1.67s** (18.0x real-time) |
| **H2: AudioSet Tagger** | PANNs CNN14 | `qiuqiangkong/audioset_tagging_cnn` | ~320 MB | MIT | Yes | **792.0 MB** reserved (464.8 MB alloc) | **0.66s** (45.6x real-time) |
| **H2: Music Mood / Copyright** | LAION-CLAP | `laion/clap-htsat-unfused` | ~600 MB | Apache 2.0 | Yes | **706.0 MB** reserved (651.0 MB alloc) | **0.67s** (44.5x real-time) |
| **H3: Diarization** | pyannote 3.1 | `pyannote/speaker-diarization-3.1` | ~25 MB | MIT | Yes (Attribution req) | *Gated (Requires HF_TOKEN)* | *Gated (Requires HF_TOKEN)* |
| **H3/H4: Speaker Diarization / Linking** | ECAPA-TDNN | `speechbrain/spkrec-ecapa-voxceleb` | ~80 MB | Apache 2.0 | Yes | **496.0 MB** reserved (457.6 MB alloc) | **0.20s** (152.1x real-time) |
| **H6: Transcription** | faster-whisper turbo | `Systran/faster-whisper-large-v3-turbo` | ~1.6 GB | MIT / Apache 2.0 | Yes | **1.6 GB** (CTranslate2 pool) | **1.51s** (19.8x real-time) |
| **H7: Speech Emotion** | emotion2vec+ base | `iic/emotion2vec_plus_base` | ~360 MB | Apache 2.0 | Yes | **1,266.0 MB** reserved (1021.2 MB alloc) | **0.55s** (54.8x real-time) |
| **H7: Prosody Dynamics** | DSP F0 & Energy | Deterministic DSP (pyin F0 + RMS) | 0 MB | BSD-3-Clause | Yes | **0.0 MB** (CPU DSP) | **11.12s** (2.7x real-time) |
| **H8: Narrative & Grouping** | Qwen 2.5 7B / 14B | Local Ollama (`qwen3:14b` / `qwen2.5:7b`) | ~4.7 - 9.3 GB | Apache 2.0 | Yes | **4.7 - 9.3 GB** (Ollama offload) | **~35 tok/s** |
| **M2: Face & Landmarks** | YuNet | `opencv/opencv_zoo` (`face_detection_yunet_2023mar.onnx`) | 395 KB | Apache 2.0 | Yes | **0.0 MB** (CPU / DNN) | **5.63s** / 60 frames (5.3x real-time) |
| **M2: Face Verification** | SFace | `opencv/opencv_zoo` (`face_recognition_sface_2021dec.onnx`) | 37.4 MB | Apache 2.0 | Yes | **0.0 MB** (CPU / DNN) | **0.37s** / 60 crops (80.0x real-time) |
| **M2: Person Detector** | RT-DETR-R18 | `PekingU/rtdetr_r18vd` (clean non-AGPL) | ~80 MB | Apache 2.0 | Yes | **2,172.0 MB** reserved (1142.6 MB alloc) | **4.46s** / 60 frames (6.7x real-time) |
| **M2: Scene Transitions** | TransNetV2 | `soCzech/TransNetV2` (elya5/transnetv2.onnx) | ~31 MB | MIT | Yes | **4.0 MB** (ONNX Runtime) | **6.81s** / 900 frames (4.4x real-time) |
| **M2: Subtitle / On-Screen OCR** | PP-OCRv4 | `RapidAI/RapidOCR` (`rapidocr_onnxruntime`) | ~15 MB | Apache 2.0 | Yes | **4.0 MB** (ONNX Runtime) | **100.42s** / 60 full 720p frames |
| **M2: Visual Description** | Qwen2.5-VL / Local Vision | Ollama / API Vision | ~4.5 - 9 GB | Apache 2.0 | Yes | Offload via Ollama / API | Event-driven (key moments only) |

---

## 2. Model Slot Architecture & Configuration (Rule R2)

Every model above represents a swappable slot configured in `config/clipper.yaml`. No model class is hard-coded into the pipeline:

```yaml
engine: "legacy" # legacy | script (stays legacy until user approval)

hearing:
  stem_splitter: "demucs"       # facebookresearch/demucs (htdemucs)
  vad: "silero"                 # snakers4/silero-vad
  audio_tagger: "panns"         # panns_inference (CNN14) or beats
  music_mood: "clap"            # laion/larger_clap_music_and_speech
  diarizer: "pyannote"          # pyannote/speaker-diarization-3.1
  embedder: "ecapa"             # speechbrain/spkrec-ecapa-voxceleb
  transcriber: "faster-whisper" # Systran/faster-whisper-large-v3-turbo
  emotion: "emotion2vec"        # iic/emotion2vec_plus_base
  prosody: "dsp"                # Deterministic F0 pitch & RMS loudness dynamics
  llm_narrative: "qwen2.5:7b"   # Narrative utterance role & scene segmentation pass

sight:
  face_detector: "yunet"        # OpenCV YuNet (face_detection_yunet_2023mar.onnx)
  face_recognizer: "sface"      # OpenCV SFace (face_recognition_sface_2021dec.onnx)
  person_detector: "rtdetr"     # RT-DETR-R18 / YOLOX (Apache 2.0 license-clean)
  scene_detector: "transnetv2"  # TransNetV2 cut/boundary detector
  ocr: "rapidocr"               # RapidOCR PP-OCRv4 ONNX
  vlm: "qwen2.5-vl:7b"          # Local Qwen2.5-VL via Ollama or Cloud Cascade fallback

moments:
  max_clip_length: 360.0        # Hard cap 6 minutes per clip
  max_joined_pieces: 3          # Max 3 disjoint pieces for multi-part setup/payoff joining
```

---

## 3. GPU Memory Management (Rule R4)

- **Total Physical VRAM:** 12,227 MiB (~11.9 GB usable).
- **Execution Model:** Strict stage-by-stage sequential processing over the full stream timeline.
- **Stage Lifecycle:**
  1. Load Stage Model onto CUDA.
  2. Execute pass over entire stream (or timeline chunks).
  3. Flush model, invoke `del model`, `torch.cuda.empty_cache()`, and `gc.collect()`.
  4. Verify VRAM returns to baseline before loading the next stage.
- **Never swap models per chunk.**
