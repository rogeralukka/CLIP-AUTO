import numpy as np
from clipper.hearing.prosody import analyse

SR = 16000

def tone(f, d, amp=0.3):
    t = np.arange(int(d * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)

def test_pitch_and_relative_levels():
    y = np.concatenate([tone(120, 2), np.zeros(SR, np.float32), tone(240, 2, 0.6),
                        np.zeros(SR, np.float32), tone(120, 2)])
    r = analyse(y, SR, [(0, 2, "SPEED"), (3, 5, "SPEED"), (6, 8, "SPEED")])
    assert len(r) == 3
    assert abs(r[0].pitch_hz - 120) < 5 and abs(r[1].pitch_hz - 240) < 8
    # the middle segment is an octave up and ~6 dB louder than this speaker's normal
    assert 10 < r[1].pitch_vs_normal_st < 14
    assert 4 < r[1].loudness_vs_normal_db < 8

def test_speakers_have_separate_baselines():
    y = np.concatenate([tone(110, 2), tone(220, 2)])
    r = analyse(y, SR, [(0, 2, "A"), (2, 4, "B")])
    # each speaker compared only with themselves -> both at their own normal
    assert all(abs(x.pitch_vs_normal_st) < 0.5 for x in r)

def test_silence_and_tiny_segments():
    y = np.zeros(3 * SR, np.float32)
    r = analyse(y, SR, [(0, 2, "A"), (2, 2.1, "A")])
    assert len(r) == 1 and r[0].pitch_hz is None
