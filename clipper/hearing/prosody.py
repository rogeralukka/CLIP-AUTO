"""H7 prosody: pitch and loudness per speech segment, relative to each speaker's own normal.

Why this file exists: the M0 benchmark ran librosa.pyin over the whole audio. pyin is
accurate but slow (~2.7x real time on CPU), which would cost ~3.5 h on a 9 h stream.
Here we
  * only analyse speech segments (from VAD / diarization), never music or silence, and
  * use YIN (same idea as pyin, no HMM), which is many times faster, plus an energy gate
    to throw away unvoiced frames.
The output is per segment, so it can be joined to utterances later.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

FMIN = 65.0     # Hz, low male voice
FMAX = 1000.0   # Hz, screams / very high voice
HOP_S = 0.01    # 10 ms frames


@dataclass
class SegmentProsody:
    start: float
    end: float
    speaker: Optional[str]
    pitch_hz: Optional[float]        # median pitch of voiced frames
    pitch_range_st: Optional[float]  # spread of pitch in semitones (p90 - p10)
    loudness_db: float               # mean RMS level, dBFS
    voiced_ratio: float              # share of frames with a usable pitch
    # filled by relative_to_speaker(): how this segment compares to the same speaker's normal
    pitch_vs_normal_st: Optional[float] = None
    loudness_vs_normal_db: Optional[float] = None

    def to_dict(self) -> Dict:
        return asdict(self)


def _frame_params(sr: int) -> Tuple[int, int]:
    hop = max(1, int(round(sr * HOP_S)))
    # YIN needs a frame longer than two periods of the lowest pitch
    frame = 1
    while frame < 2.2 * sr / FMIN:
        frame *= 2
    return frame, hop


def segment_prosody(y: np.ndarray, sr: int, start: float, end: float,
                    speaker: Optional[str] = None) -> SegmentProsody:
    """Pitch + loudness for one speech segment of mono audio y (float, -1..1)."""
    import librosa

    a = max(0, int(start * sr))
    b = min(len(y), int(end * sr))
    seg = np.asarray(y[a:b], dtype=np.float32)
    frame, hop = _frame_params(sr)
    if len(seg) < frame:
        seg = np.pad(seg, (0, frame - len(seg)))

    rms = librosa.feature.rms(y=seg, frame_length=frame, hop_length=hop, center=True)[0]
    rms_db = 20.0 * np.log10(np.maximum(rms, 1e-6))
    loud_db = float(np.mean(rms_db)) if len(rms_db) else -120.0

    f0 = librosa.yin(seg, fmin=FMIN, fmax=FMAX, sr=sr, frame_length=frame,
                     hop_length=hop, center=True)
    n = min(len(f0), len(rms_db))
    f0, gate_db = f0[:n], rms_db[:n]
    # voiced = loud enough relative to this segment and not stuck at the search limits
    thresh = np.percentile(gate_db, 30) if n else 0.0
    voiced = (gate_db >= max(thresh, -50.0)) & (f0 > FMIN * 1.05) & (f0 < FMAX * 0.95)
    vf = f0[voiced]
    if len(vf) >= 5:
        st = 12.0 * np.log2(vf / 55.0)
        pitch = float(np.median(vf))
        spread = float(np.percentile(st, 90) - np.percentile(st, 10))
    else:
        pitch, spread = None, None
    return SegmentProsody(start=float(start), end=float(end), speaker=speaker,
                          pitch_hz=pitch, pitch_range_st=spread, loudness_db=loud_db,
                          voiced_ratio=float(voiced.mean()) if n else 0.0)


def analyse(y: np.ndarray, sr: int,
            segments: Iterable[Sequence]) -> List[SegmentProsody]:
    """segments: (start, end) or (start, end, speaker). Only these spans are analysed."""
    out = []
    for s in segments:
        start, end = float(s[0]), float(s[1])
        spk = s[2] if len(s) > 2 else None
        if end - start < 0.15:
            continue
        out.append(segment_prosody(y, sr, start, end, spk))
    relative_to_speaker(out)
    return out


def relative_to_speaker(items: List[SegmentProsody]) -> None:
    """Compare each segment with the same speaker's median, so 'loud' means loud FOR THEM."""
    by_spk: Dict[Optional[str], List[SegmentProsody]] = {}
    for it in items:
        by_spk.setdefault(it.speaker, []).append(it)
    for group in by_spk.values():
        pitches = [g.pitch_hz for g in group if g.pitch_hz]
        base_p = float(np.median(pitches)) if pitches else None
        base_l = float(np.median([g.loudness_db for g in group]))
        for g in group:
            if base_p and g.pitch_hz:
                g.pitch_vs_normal_st = round(12.0 * float(np.log2(g.pitch_hz / base_p)), 2)
            g.loudness_vs_normal_db = round(g.loudness_db - base_l, 2)
