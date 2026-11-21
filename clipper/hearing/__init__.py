"""Hearing Engine Package for CLIP AUTO.

Extracts rich audio understanding from video/stream broadcasts:
H1: Voice/BGM separation & VAD (Demucs, Silero)
H2: Sound events & Music mood/copyright (AudioSet PANNs/BEATs, CLAP)
H3: Speaker diarization across long streams (pyannote, chunk linking)
H4: Voice gallery identification & enrollment (ECAPA-TDNN)
H5: Live mic vs Media/Game audio classification
H6: Speech-to-text transcription (faster-whisper)
H7: Speech emotion & prosody dynamics (emotion2vec+, DSP pitch/energy)
H8: Utterance narrative roles & Scene grouping (LLM pass)
"""
