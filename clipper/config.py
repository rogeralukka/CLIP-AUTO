"""Configuration loader for Stream Clipper."""
import copy
import os
import yaml
from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "audio": {
        "whisper_model": "large-v3-turbo",
        "whisper_batch_size": 16,
        "vad_filter": True,
        "section_padding_seconds": 3.0,
    },
    "signals": {
        "use_story_hunter": True,
        "max_clips_per_hour": 4,
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
        "box_padding": 1.0,
        "fullscreen_face_ratio": 0.22,
        "facecam_max_drift": 0.03,
        "gameplay_aspect": "16:9",
        "split_facecam_height": 640,
        "gameplay_blur_background": True,
    },
    "polish": {
        "progress_bar": True,
        "progress_bar_color": "yellow",
        "hook_duration_seconds": 2.5,
        "punch_zoom": {
            "enabled": True,
            "factor": 1.12,
        },
        "loudnorm": {
            "enabled": True,
            "target_lufs": -14.0,
            "target_tp": -1.0,
            "target_lra": 11.0,
        },
        "silence": {
            "trim_breath_seconds": 0.3,
            "remove_internal_dead_air": True,
            "dead_air_min_gap_seconds": 0.40,
            "dead_air_target_gap_seconds": 0.15,
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

    cfg = copy.deepcopy(DEFAULT_CONFIG)
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

    _LOADED_CONFIG = _merge_overrides(cfg)
    return _LOADED_CONFIG


def get_overrides_path() -> str:
    return os.path.join(os.path.dirname(get_config_path()), "user_overrides.yaml")


def _merge_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = get_overrides_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                ov = yaml.safe_load(f) or {}
            for k, v in ov.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception:
            pass
    return cfg


def save_user_override(section: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Persist settings changed in the web UI to config/user_overrides.yaml.

    Kept separate so config/clipper.yaml (and its comments) is never rewritten.
    """
    path = get_overrides_path()
    ov: Dict[str, Any] = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            ov = yaml.safe_load(f) or {}
    ov.setdefault(section, {}).update(values)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(ov, f, sort_keys=False)
    return load_config(reload=True)
