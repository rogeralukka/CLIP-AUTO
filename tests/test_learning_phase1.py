"""Unit tests for Phase 1: Learning Collector & Audio Cross-Correlation Matcher."""

from __future__ import annotations

import os
import tempfile
import unittest
import numpy as np
import scipy.io.wavfile

from clipper.learning.matcher import (
    compute_energy_envelope,
    normalized_cross_correlation,
    match_clip_in_stream,
    find_coarse_offset,
)
from clipper.learning.collector import get_gta6_clips


class TestLearningPhase1Matcher(unittest.TestCase):
    def setUp(self):
        self.sr = 16000
        self.test_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.test_dir.cleanup()

    def _generate_synthetic_audio(self, duration_sec: float, seed: int = 42) -> np.ndarray:
        np.random.seed(seed)
        t = np.linspace(0, duration_sec, int(duration_sec * self.sr), endpoint=False)
        # Combine harmonic voice-like tones + speech formants
        signal = (
            0.5 * np.sin(2 * np.pi * 220 * t)
            + 0.3 * np.sin(2 * np.pi * 440 * t)
            + 0.2 * np.sin(2 * np.pi * 880 * t)
        )
        # Modulate amplitude with speech-like rhythm (3 Hz syllabic cadence)
        envelope = 0.5 * (1.0 + np.sin(2 * np.pi * 3.0 * t))
        signal *= envelope
        # Add slight white noise
        signal += 0.05 * np.random.normal(0, 1, len(signal))
        return signal.astype(np.float32)

    def test_normalized_cross_correlation_exact_match(self):
        """Identical slice should produce correlation ~1.0 at lag 0."""
        signal = self._generate_synthetic_audio(10.0)
        clip = signal[int(2.0 * self.sr) : int(6.0 * self.sr)]
        stream = signal[int(0.0 * self.sr) : int(10.0 * self.sr)]

        peak, best_lag = normalized_cross_correlation(stream, clip)
        expected_lag = int(2.0 * self.sr)
        self.assertAlmostEqual(peak, 1.0, places=2)
        self.assertEqual(best_lag, expected_lag)

    def test_energy_envelope_downsampling(self):
        """Envelope downsampling should preserve coarse speech burst timings."""
        signal = self._generate_synthetic_audio(10.0)
        env = compute_energy_envelope(signal, self.sr, target_hz=500)
        expected_len = int(10.0 * 500)
        self.assertAlmostEqual(len(env), expected_len, delta=5)
        # Check standard normalized properties
        self.assertAlmostEqual(float(env.mean()), 0.0, places=1)
        self.assertAlmostEqual(float(env.std()), 1.0, places=1)

    def test_coarse_offset_finding(self):
        """Coarse envelope correlation should locate offset within +/- 0.5s."""
        stream = self._generate_synthetic_audio(30.0)
        clip = stream[int(12.0 * self.sr) : int(16.0 * self.sr)]

        s_env = compute_energy_envelope(stream, self.sr, target_hz=500)
        c_env = compute_energy_envelope(clip, self.sr, target_hz=500)

        candidates = find_coarse_offset(s_env, c_env, env_hz=500, top_k=3)
        self.assertTrue(len(candidates) > 0)
        closest_cand = min(candidates, key=lambda c: abs(c - 12.0))
        self.assertAlmostEqual(closest_cand, 12.0, delta=0.5)

    def test_match_clip_in_stream_clean_alignment(self):
        """End-to-end alignment on synthetic WAV files should achieve confidence > 0.85."""
        stream_data = self._generate_synthetic_audio(40.0)
        # Extract 8s clip starting at t=15.0s
        clip_data = stream_data[int(15.0 * self.sr) : int(23.0 * self.sr)]

        stream_wav = os.path.join(self.test_dir.name, "stream.wav")
        clip_wav = os.path.join(self.test_dir.name, "clip.wav")

        scipy.io.wavfile.write(stream_wav, self.sr, (stream_data * 32767).astype(np.int16))
        scipy.io.wavfile.write(clip_wav, self.sr, (clip_data * 32767).astype(np.int16))

        res = match_clip_in_stream(
            clip_wav=clip_wav,
            stream_wav=stream_wav,
            clip_id="test_clip_01",
            stream_id="test_stream_01",
            chunk_sec=4.0,
            chunk_overlap=0.5,
        )

        self.assertTrue(res.matched)
        self.assertGreaterEqual(res.confidence, 0.85)
        self.assertAlmostEqual(res.stream_start, 15.0, delta=0.5)
        self.assertAlmostEqual(res.stream_end, 23.0, delta=0.5)

    def test_match_clip_in_stream_rejects_uncorrelated(self):
        """Uncorrelated audio must be rejected with matched=False."""
        stream_data = self._generate_synthetic_audio(30.0, seed=101)
        # Generate independent high-frequency noise/tones completely different from stream
        np.random.seed(999)
        t = np.linspace(0, 6.0, int(6.0 * self.sr), endpoint=False)
        clip_data = 0.5 * np.sin(2 * np.pi * 3500 * t) + 0.1 * np.random.normal(0, 1, len(t))
        clip_data = clip_data.astype(np.float32)

        stream_wav = os.path.join(self.test_dir.name, "stream_uncorr.wav")
        clip_wav = os.path.join(self.test_dir.name, "clip_uncorr.wav")

        scipy.io.wavfile.write(stream_wav, self.sr, (stream_data * 32767).astype(np.int16))
        scipy.io.wavfile.write(clip_wav, self.sr, (clip_data * 32767).astype(np.int16))

        res = match_clip_in_stream(
            clip_wav=clip_wav,
            stream_wav=stream_wav,
            clip_id="test_clip_bad",
            stream_id="test_stream_01",
        )

        self.assertFalse(res.matched)
        self.assertLess(res.confidence, 0.40)


class TestLearningPhase1Collector(unittest.TestCase):
    def test_get_gta6_clips_filtering(self):
        mock_videos = [
            {"id": "v1", "title": "iShowSpeed Meets Niko Bellic In GTA 6"},
            {"id": "v2", "title": "SPEED PLAYS FORTNITE CHAPTER 5"},
            {"id": "v3", "title": "Speed rage quit on GTA6 online"},
            {"id": "v4", "title": "GTA VI LEAKS ARE INSANE"},
        ]
        gta = get_gta6_clips(mock_videos)
        self.assertEqual(len(gta), 3)
        self.assertEqual({x["id"] for x in gta}, {"v1", "v3", "v4"})


if __name__ == "__main__":
    unittest.main()
