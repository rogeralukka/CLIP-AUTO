"""Audio Cross-Correlation & Alignment Matcher for CLIP AUTO.

Matches short edited clips (45s - 3min) against long raw stream VODs (up to 24 hours)
using chunk-based normalized cross-correlation. Robust to jump cuts, background music,
and slight audio level adjustments.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import scipy.signal
import scipy.io.wavfile


@dataclass
class ChunkMatch:
    clip_start: float
    clip_end: float
    stream_start: float
    stream_end: float
    correlation: float
    offset: float  # stream_start - clip_start


@dataclass
class ClipAlignmentResult:
    clip_id: str
    stream_id: str
    clip_duration: float
    stream_duration: float
    matched: bool
    confidence: float
    stream_start: float
    stream_end: float
    matched_duration: float
    chunk_matches: List[ChunkMatch]
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


def load_wav_16k_mono(wav_path: str | Path) -> Tuple[np.ndarray, int]:
    """Load a 16 kHz mono WAV file as a float32 numpy array normalized to [-1, 1]."""
    sr, data = scipy.io.wavfile.read(str(wav_path))
    if data.ndim > 1:
        data = data.mean(axis=1)

    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        data = data.astype(np.float32) / 2147483648.0
    elif data.dtype == np.uint8:
        data = (data.astype(np.float32) - 128.0) / 128.0
    else:
        data = data.astype(np.float32)

    return data, sr


def compute_energy_envelope(
    signal: np.ndarray,
    sr: int,
    target_hz: int = 500,
) -> np.ndarray:
    """Downsample audio signal magnitude envelope to target_hz for fast coarse search."""
    hop = max(1, sr // target_hz)
    # Rectified smoothed signal
    abs_sig = np.abs(signal)
    kernel_size = max(3, hop)
    # Simple moving average filter
    kernel = np.ones(kernel_size, dtype=np.float32) / kernel_size
    smoothed = scipy.signal.convolve(abs_sig, kernel, mode="same")
    envelope = smoothed[::hop].astype(np.float32)
    # Normalize
    envelope -= envelope.mean()
    std = envelope.std()
    if std > 1e-6:
        envelope /= std
    return envelope


def normalized_cross_correlation(
    stream_chunk: np.ndarray,
    clip_chunk: np.ndarray,
) -> Tuple[float, int]:
    """Compute normalized cross-correlation between a short clip chunk and a stream slice.
    
    Returns (peak_correlation, best_lag_samples).
    """
    n_clip = len(clip_chunk)
    n_stream = len(stream_chunk)
    if n_stream < n_clip:
        return 0.0, 0

    c_norm = clip_chunk - np.mean(clip_chunk)
    c_std = np.std(c_norm)
    if c_std < 1e-6:
        return 0.0, 0
    c_norm /= (c_std * np.sqrt(n_clip))

    # FFT-based correlation
    corr = scipy.signal.fftconvolve(stream_chunk, c_norm[::-1], mode="valid")

    # Local normalization for the stream slice
    # Compute running mean and variance using uniform filter or cumulative sum
    cum = np.pad(np.cumsum(stream_chunk), (1, 0))
    cum2 = np.pad(np.cumsum(stream_chunk ** 2), (1, 0))
    window_sum = cum[n_clip:] - cum[:-n_clip]
    window_sum2 = cum2[n_clip:] - cum2[:-n_clip]

    means = window_sum / n_clip
    variances = (window_sum2 / n_clip) - (means ** 2)
    variances[variances < 1e-9] = 1e-9
    stds = np.sqrt(variances) * np.sqrt(n_clip)

    valid_corr = corr[:len(stds)] / stds
    best_idx = int(np.argmax(valid_corr))
    peak_val = float(valid_corr[best_idx])
    return peak_val, best_idx


def find_coarse_offset(
    stream_env: np.ndarray,
    clip_env: np.ndarray,
    env_hz: int,
    top_k: int = 5,
) -> List[float]:
    """Find top candidate stream offsets (in seconds) using envelope cross-correlation."""
    n_clip = len(clip_env)
    n_stream = len(stream_env)
    if n_stream < n_clip:
        return [0.0]

    # fftconvolve mode='valid'
    corr = scipy.signal.fftconvolve(stream_env, clip_env[::-1], mode="valid")
    # Find local peaks
    peaks, _ = scipy.signal.find_peaks(corr, distance=int(env_hz * 2))
    if len(peaks) == 0:
        # Fall back to argmax
        best_indices = [int(np.argmax(corr))]
    else:
        # Sort peaks by correlation value descending
        peak_values = corr[peaks]
        sorted_order = np.argsort(peak_values)[::-1]
        best_indices = peaks[sorted_order[:top_k]].tolist()

    return [idx / float(env_hz) for idx in best_indices]


def match_clip_in_stream(
    clip_wav: str | Path,
    stream_wav: str | Path,
    clip_id: str = "",
    stream_id: str = "",
    chunk_sec: float = 4.0,
    chunk_overlap: float = 0.5,
    min_confidence: float = 0.65,
    min_matched_ratio: float = 0.50,
) -> ClipAlignmentResult:
    """Perform robust chunk-based alignment of clip inside stream audio."""
    clip_audio, sr_clip = load_wav_16k_mono(clip_wav)
    stream_audio, sr_stream = load_wav_16k_mono(stream_wav)

    if sr_clip != sr_stream:
        raise ValueError(f"Sample rates do not match: clip={sr_clip}, stream={sr_stream}")

    sr = sr_clip
    clip_dur = len(clip_audio) / float(sr)
    stream_dur = len(stream_audio) / float(sr)

    if clip_dur > stream_dur:
        return ClipAlignmentResult(
            clip_id=clip_id,
            stream_id=stream_id,
            clip_duration=clip_dur,
            stream_duration=stream_dur,
            matched=False,
            confidence=0.0,
            stream_start=0.0,
            stream_end=0.0,
            matched_duration=0.0,
            chunk_matches=[],
            notes="Clip is longer than stream",
        )

    # 1. Coarse search over envelopes to narrow down the search window
    env_hz = 500
    clip_env = compute_energy_envelope(clip_audio, sr, target_hz=env_hz)
    stream_env = compute_energy_envelope(stream_audio, sr, target_hz=env_hz)

    candidate_offsets = find_coarse_offset(stream_env, clip_env, env_hz=env_hz, top_k=5)

    # 2. Divide clip into chunks (e.g. 4.0s with 2.0s hop)
    chunk_samples = int(chunk_sec * sr)
    hop_samples = int(chunk_sec * (1.0 - chunk_overlap) * sr)

    chunks: List[Tuple[float, float, np.ndarray]] = []
    for start_samp in range(0, len(clip_audio) - chunk_samples + 1, hop_samples):
        end_samp = start_samp + chunk_samples
        t_start = start_samp / float(sr)
        t_end = end_samp / float(sr)
        chunks.append((t_start, t_end, clip_audio[start_samp:end_samp]))

    if not chunks:
        # If clip is shorter than 4s, treat entire clip as one chunk
        chunks.append((0.0, clip_dur, clip_audio))

    # 3. For each candidate offset, verify chunks in a local window [offset - 30s, offset + clip_dur + 30s]
    best_alignment: Optional[Tuple[float, List[ChunkMatch], float, float]] = None
    best_overall_score = -1.0

    for cand_sec in candidate_offsets:
        win_start_sec = max(0.0, cand_sec - 30.0)
        win_end_sec = min(stream_dur, cand_sec + clip_dur + 30.0)
        start_samp = int(win_start_sec * sr)
        end_samp = int(win_end_sec * sr)
        local_stream = stream_audio[start_samp:end_samp]

        current_matches: List[ChunkMatch] = []
        for c_start, c_end, c_chunk in chunks:
            peak_val, best_lag = normalized_cross_correlation(local_stream, c_chunk)
            if peak_val >= min_confidence:
                matched_stream_start = win_start_sec + (best_lag / float(sr))
                matched_stream_end = matched_stream_start + (len(c_chunk) / float(sr))
                current_matches.append(
                    ChunkMatch(
                        clip_start=c_start,
                        clip_end=c_end,
                        stream_start=matched_stream_start,
                        stream_end=matched_stream_end,
                        correlation=round(peak_val, 4),
                        offset=round(matched_stream_start - c_start, 4),
                    )
                )

        if not current_matches:
            continue

        # Check temporal consistency: offsets delta_i = stream_start - clip_start
        # Offsets should be tightly clustered (allowing slight jumps for jump cuts)
        offsets = [m.offset for m in current_matches]
        median_offset = float(np.median(offsets))

        # Filter inliers within +/- 2.0s of median
        inliers = [m for m in current_matches if abs(m.offset - median_offset) <= 2.5]
        if not inliers:
            continue

        # Score is fraction of chunks matched * average inlier correlation
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
            clip_duration=clip_dur,
            stream_duration=stream_dur,
            matched=False,
            confidence=0.0,
            stream_start=0.0,
            stream_end=0.0,
            matched_duration=0.0,
            chunk_matches=[],
            notes="No matching chunks found above threshold",
        )

    score, inliers, st_start, st_end, match_ratio, avg_corr = best_alignment
    if match_ratio < min_matched_ratio or avg_corr < min_confidence or score < 0.50:
        return ClipAlignmentResult(
            clip_id=clip_id,
            stream_id=stream_id,
            clip_duration=clip_dur,
            stream_duration=stream_dur,
            matched=False,
            confidence=round(score, 4),
            stream_start=0.0,
            stream_end=0.0,
            matched_duration=0.0,
            chunk_matches=[],
            notes=f"Insufficient match criteria: ratio={match_ratio:.2f}, avg_corr={avg_corr:.2f}, score={score:.2f}",
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
        notes=f"Successfully matched with {len(inliers)}/{len(chunks)} chunks (avg corr={np.mean([m.correlation for m in inliers]):.3f})",
    )
