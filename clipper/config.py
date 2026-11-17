"""Configuration loader for Stream Clipper."""
import os
import yaml
from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "audio": {
        "whisper_model": "large-v3",
        "vad_filter": True,
        "section_padding_seconds": 3.0,
    },
    "signals": {
        "rolling_window_seconds": 600,
        "merge_gap_seconds": 15,
        "max_lead_seconds": 5.0,
        "max_trail_seconds": 5.0,
        "weights": {
            "chat_rate": 0.25,
            "chat_keywords": 0.30,
            "streamer_loudness": 0.20,
            "speech_energy": 0.15,
            "youtube_heatmap": 0.10,
        },
        "z_thresholds": {
            "Low": 2.5,
            "Medium": 2.0,
            "High": 1.5,
        },
        "default_sensitivity": "Medium",
        "safety_max_clips": 50,
        "dedupe_overlap_threshold": 0.50,
    },
    "llm": {
        "enabled": False,
        "endpoint": "http://localhost:11434",
        "model": "qwen2.5:7b",
        "candidate_multiplier": 3,
        "weight_llm": 0.50,
        "weight_hype": 0.50,
        "timeout_seconds": 30,
        "retry_count": 1,
    },
    "facecam": {
        "sample_interval_seconds": 1.0,
        "presence_threshold": 0.60,
        "box_padding": 0.25,
        "split_facecam_height": 640,
        "gameplay_blur_background": True,
    },
    "polish": {
        "hook_duration_seconds": 2.5,
        "loudnorm": {
            "enabled": True,
            "target_lufs": -14.0,
            "target_tp": -1.0,
            "target_lra": 11.0,
        },
        "silence": {
            "trim_breath_seconds": 0.3,
            "remove_internal_dead_air": False,
            "dead_air_gap_seconds": 1.0,
        },
    },
    "qc": {
        "loudness_tolerance_lu": 2.0,
        "music_silence_words_duration": 8.0,
    },
}

_LOADED_CONFIG: Dict[str, Any] = None


def get_config_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "config", "clipper.yaml")


def load_config(reload: bool = False) -> Dict[str, Any]:
    global _LOADED_CONFIG
    if _LOADED_CONFIG is not None and not reload:
        return _LOADED_CONFIG

    cfg = dict(DEFAULT_CONFIG)
    path = get_config_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
                # Deep merge top levels
                for k, v in user_cfg.items():
                    if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
        except Exception:
            pass

    _LOADED_CONFIG = cfg
    return _LOADED_CONFIG
