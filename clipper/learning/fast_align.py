"""Fast Blocked Audio Cross-Correlation Alignment Runner.

Uses a 500 Hz downsampled energy envelope for sub-second whole-stream coarse search,
followed by sample-accurate random-access WAV slices for fine chunk verification.
Matches 2-minute clips across 24-hour streams in ~0.2s per clip.
"""

from __future__ import annotations

import json
import os
import struct
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import scipy.io.wavfile
import scipy.signal

from clipper.learning.collector import AUDIO_DIR, MATCHES_DIR, METADATA_DIR
from clipper.learning.matcher import (
    ChunkMatch,
    ClipAlignmentResult,
    compute_energy_envelope,
    normalized_cross_correlation,
)


def read_wav_header(wav_path: str | Path) -> Tuple[int, int, int]:
    """Read sample_rate, channels, and total_samples from a standard WAV file header."""
    with open(wav_path, "rb") as f:
        riff = f.read(12)
        if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise ValueError(f"Not a valid RIFF/WAVE file: {wav_path}")

        # Scan chunks
        fmt_found = False
        data_found = False
        sample_rate = 16000
        channels = 1
        data_size = 0
        data_pos = 44

        while True:
            chunk_header = f.read(8)
            if len(chunk_header) < 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
            if chunk_id == b"fmt ":
                fmt_data = f.read(chunk_size)
                fmt_tag, channels, sample_rate, _, _, bits_per_sample = struct.unpack("<HHIIHH", fmt_data[:16])
                fmt_found = True
            elif chunk_id == b"data":
                data_size = chunk_size
                data_pos = f.tell()
                data_found = True
                break
            else:
                f.seek(chunk_size, os.SEEK_CUR)

    if not data_found:
        raise ValueError(f"No data chunk found in {wav_path}")

    total_samples = data_size // (channels * 2)
    return sample_rate, channels, total_samples, data_pos


def read_wav_slice_fast(
    wav_path: str | Path,
    start_sec: float,
    dur_sec: float,
    data_pos: int,
    sr: int = 16000,
) -> np.ndarray:
    """Read a precise slice from a 16 kHz mono 16-bit WAV file in < 1ms."""
    start_samp = max(0, int(start_sec * sr))
    n_samples = max(0, int(dur_sec * sr))
    start_byte = data_pos + (start_samp * 2)
    byte_count = n_samples * 2

    with open(wav_path, "rb") as f:
        f.seek(start_byte)
        raw = f.read(byte_count)

    if len(raw) < 4:
        return np.zeros(0, dtype=np.float32)

    return (np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)


def get_or_compute_stream_envelope(
    wav_path: str | Path,
    env_hz: int = 500,
) -> Tuple[np.ndarray, int, float]:
    """Compute and cache the 500 Hz downsampled RMS envelope for a 24h stream."""
    wav_path = Path(wav_path)
    stream_id = wav_path.stem
    cache_path = wav_path.parent / f"{stream_id}_env{env_hz}.npy"

    sr, channels, total_samples, data_pos = read_wav_header(wav_path)
    total_dur = total_samples / float(sr)

    if cache_path.is_file():
        env = np.load(cache_path)
        return env, data_pos, total_dur

    print(f"Computing {env_hz} Hz envelope for {wav_path.name} ({total_dur / 3600:.2f} hours)...")
    t0 = time.time()

    hop = sr // env_hz  # e.g. 16000 // 500 = 32 samples per envelope point
    block_sec = 300.0  # 5 minutes per block
    block_samples = int(block_sec * sr)

    env_chunks = []
    with open(wav_path, "rb") as f:
        f.seek(data_pos)
        while True:
            raw = f.read(block_samples * 2)
            if not raw:
                break
            block = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            # Truncate to multiple of hop
            n_hops = len(block) // hop
            if n_hops <= 0:
                break
            reshaped = np.abs(block[:n_hops * hop]).reshape(n_hops, hop)
            chunk_env = reshaped.mean(axis=1)
            env_chunks.append(chunk_env)

    full_env = np.concatenate(env_chunks).astype(np.float32)
    # Standardize
    full_env -= full_env.mean()
    std = full_env.std()
    if std > 1e-6:
        full_env /= std

    np.save(cache_path, full_env)
    print(f"Computed & cached envelope in {time.time() - t0:.2f}s! ({len(full_env):,} points, {cache_path.stat().st_size / 1e6:.1f} MB)")
    return full_env, data_pos, total_dur


def match_single_clip(
    clip_wav: Path,
    stream_wav: Path,
    stream_env: np.ndarray,
    data_pos: int,
    stream_dur: float,
    clip_id: str,
    stream_id: str,
    env_hz: int = 500,
    chunk_sec: float = 4.0,
    chunk_overlap: float = 0.5,
    min_confidence: float = 0.60,
    min_matched_ratio: float = 0.40,
) -> ClipAlignmentResult:
    sr_clip, clip_audio = scipy.io.wavfile.read(str(clip_wav))
    if clip_audio.ndim > 1:
        clip_audio = clip_audio.mean(axis=1)
    if clip_audio.dtype == np.int16:
        clip_audio = clip_audio.astype(np.float32) / 32768.0

    clip_dur = len(clip_audio) / float(sr_clip)
    clip_env = compute_energy_envelope(clip_audio, sr_clip, target_hz=env_hz)

    # Coarse search via fftconvolve
    corr = scipy.signal.fftconvolve(stream_env, clip_env[::-1], mode="valid")
    # Top 8 candidate peaks separated by at least 2 seconds
    peaks, _ = scipy.signal.find_peaks(corr, distance=int(env_hz * 2))
    if len(peaks) == 0:
        candidates = [int(np.argmax(corr)) / float(env_hz)]
    else:
        peak_vals = corr[peaks]
        sorted_indices = peaks[np.argsort(peak_vals)[::-1][:8]]
        candidates = [idx / float(env_hz) for idx in sorted_indices]

    # Prepare clip chunks
    sr = sr_clip
    chunk_samples = int(chunk_sec * sr)
    hop_samples = int(chunk_sec * (1.0 - chunk_overlap) * sr)

    chunks: List[Tuple[float, float, np.ndarray]] = []
    for start_samp in range(0, len(clip_audio) - chunk_samples + 1, hop_samples):
        end_samp = start_samp + chunk_samples
        t_start = start_samp / float(sr)
        t_end = end_samp / float(sr)
        chunks.append((t_start, t_end, clip_audio[start_samp:end_samp]))

    if not chunks:
        chunks.append((0.0, clip_dur, clip_audio))

    best_alignment = None
    best_overall_score = -1.0

    for cand_sec in candidates:
        current_matches: List[ChunkMatch] = []
        for c_start, c_end, c_chunk in chunks:
            # Expected position in stream based on candidate offset, allowing +/- 6s for internal cuts
            expected_pos = cand_sec + c_start
            search_start = max(0.0, expected_pos - 6.0)
            search_dur = chunk_sec + 12.0
            local_stream = read_wav_slice_fast(stream_wav, search_start, search_dur, data_pos, sr=sr)
            if len(local_stream) < chunk_samples:
                continue

            peak_val, best_lag = normalized_cross_correlation(local_stream, c_chunk)
            if peak_val >= min_confidence:
                matched_start = search_start + (best_lag / float(sr))
                matched_end = matched_start + (len(c_chunk) / float(sr))
                current_matches.append(
                    ChunkMatch(
                        clip_start=c_start,
                        clip_end=c_end,
                        stream_start=matched_start,
                        stream_end=matched_end,
                        correlation=round(peak_val, 4),
                        offset=round(matched_start - c_start, 4),
                    )
                )

        if not current_matches:
            continue

        offsets = [m.offset for m in current_matches]
        median_offset = float(np.median(offsets))
        inliers = [m for m in current_matches if abs(m.offset - median_offset) <= 2.5]
        if not inliers:
            continue

        match_ratio = len(inliers) / float(len(chunks))
        avg_corr = float(np.mean([m.correlation for m in inliers]))
        score = match_ratio * avg_corr

        if score > best_overall_score:
            best_overall_score = score
            st_start = min(m.stream_start for m in inliers)
            st_end = max(m.stream_end for m in inliers)
            best_alignment = (score, inliers, st_start, st_end, match_ratio, avg_corr)

    if best_alignment is None:
        return ClipAlignmentResult(
            clip_id=clip_id,
            stream_id=stream_id,
            clip_duration=round(clip_dur, 2),
            stream_duration=round(stream_dur, 2),
            matched=False,
            confidence=0.0,
            stream_start=0.0,
            stream_end=0.0,
            matched_duration=0.0,
            chunk_matches=[],
            notes="No matching chunks found above threshold",
        )

    score, inliers, st_start, st_end, match_ratio, avg_corr = best_alignment
    if match_ratio < min_matched_ratio or avg_corr < min_confidence or score < 0.40:
        return ClipAlignmentResult(
            clip_id=clip_id,
            stream_id=stream_id,
            clip_duration=round(clip_dur, 2),
            stream_duration=round(stream_dur, 2),
            matched=False,
            confidence=round(score, 4),
            stream_start=0.0,
            stream_end=0.0,
            matched_duration=0.0,
            chunk_matches=[],
            notes=f"Low alignment confidence: ratio={match_ratio:.2f}, avg_corr={avg_corr:.2f}, score={score:.2f}",
        )

    matched_dur = st_end - st_start
    return ClipAlignmentResult(
        clip_id=clip_id,
        stream_id=stream_id,
        clip_duration=round(clip_dur, 2),
        stream_duration=round(stream_dur, 2),
        matched=True,
        confidence=round(score, 4),
        stream_start=round(st_start, 2),
        stream_end=round(st_end, 2),
        matched_duration=round(matched_dur, 2),
        chunk_matches=inliers,
        notes=f"Matched {len(inliers)}/{len(chunks)} chunks (avg corr={avg_corr:.3f})",
    )
