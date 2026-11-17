"""Karaoke ASS subtitle generation with presets and Windows path escaping."""
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("clipper.captions")

DEFAULT_PRESET: Dict[str, Any] = {
    "name": "classic_yellow",
    "font_name": "Arial Black",
    "font_size": 88,
    "primary_colour": "&H0000FFFF",     # Yellow fill in ASS BGR (&HAABBGGRR)
    "secondary_colour": "&H00FFFFFF",   # Base white text before highlight
    "outline_colour": "&H00000000",     # Black outline
    "back_colour": "&H96000000",        # Translucent black shadow
    "bold": -1,
    "italic": 0,
    "outline": 6,
    "shadow": 2,
    "alignment": 2,                     # 2 = Bottom Center
    "margin_l": 60,
    "margin_r": 60,
    "margin_v": 520,                    # Centered in vertical safe zone
    "max_words_per_line": 5,
    "gap_break_seconds": 0.6,
}


def load_preset(preset_name_or_path: Optional[str] = None) -> Dict[str, Any]:
    """Load a caption preset by name from presets/ or from a custom file path."""
    preset = dict(DEFAULT_PRESET)
    if not preset_name_or_path:
        return preset

    # Check if exact file path
    if os.path.isfile(preset_name_or_path):
        with open(preset_name_or_path, "r", encoding="utf-8") as f:
            preset.update(json.load(f))
        return preset

    # Check in presets directory
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    clean_name = preset_name_or_path
    if not clean_name.endswith(".json"):
        clean_name += ".json"
    preset_path = os.path.join(base_dir, "presets", clean_name)

    if os.path.isfile(preset_path):
        with open(preset_path, "r", encoding="utf-8") as f:
            preset.update(json.load(f))
        logger.info(f"Loaded caption preset '{preset_name_or_path}' from {preset_path}")
    else:
        logger.warning(
            f"Preset '{preset_name_or_path}' not found at {preset_path}. Using classic_yellow default."
        )

    return preset


def fmt_ts(seconds: float) -> str:
    """Format seconds into ASS timestamp format: H:MM:SS.cs"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def extract_hook_phrase(words: List[Tuple[float, float, str]], max_words: int = 7) -> str:
    """Extract the first strong phrase from word timestamps as a fallback hook title."""
    if not words:
        return ""
    text_words = [w[2].strip() for w in words if w[2].strip()]
    if not text_words:
        return ""
    selected = []
    for w in text_words[:max_words]:
        selected.append(w)
        if any(w.endswith(punct) for punct in [".", "!", "?", ","]):
            break
    clean = " ".join(selected).rstrip(",.")
    return clean


def build_ass_header(preset: Dict[str, Any], hook_margin_v: int = 240) -> str:
    """Generate ASS script header with Cap and Hook style definitions."""
    style_line = (
        f"Style: Cap,{preset['font_name']},{preset['font_size']},"
        f"{preset['primary_colour']},{preset['secondary_colour']},"
        f"{preset['outline_colour']},{preset['back_colour']},"
        f"{preset['bold']},{preset['italic']},0,0,100,100,1,0,1,"
        f"{preset['outline']},{preset['shadow']},{preset['alignment']},"
        f"{preset['margin_l']},{preset['margin_r']},{preset['margin_v']},1"
    )

    hook_style_line = (
        f"Style: Hook,Arial Black,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
        f"-1,0,0,0,100,100,1,0,1,8,4,8,80,80,{hook_margin_v},1"
    )

    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style_line}
{hook_style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def make_ass(
    words: List[Tuple[float, float, str]],
    output_path: str,
    preset: Optional[Dict[str, Any]] = None,
    max_words: Optional[int] = None,
    gap_break: Optional[float] = None,
    layout: str = "auto",
    hook_title: Optional[str] = None,
    hook_duration: float = 2.5,
) -> str:
    """Create a word-by-word karaoke ASS subtitle file with optional hook title card.

    Args:
        words: List of (start_sec, end_sec, text) tuples.
        output_path: Path where the .ass file will be saved.
        preset: Styling dictionary (or loaded default).
        max_words: Override max words per subtitle card.
        gap_break: Override silence duration threshold to start a new subtitle card.
        layout: Vertical video layout ('split', 'gameplay', 'face', or 'auto').
        hook_title: Big bold hook text displayed for first hook_duration seconds.
        hook_duration: Duration to display hook title (default 2.5s).
    """
    if preset is None:
        preset = dict(DEFAULT_PRESET)
    else:
        preset = dict(preset)

    # Adjust vertical margin based on layout to avoid overlapping facecam
    if layout == "split":
        preset["margin_v"] = preset.get("split_margin_v", 360)
        hook_margin_v = 680  # Top of gameplay section below 640px facecam
    else:
        if layout == "gameplay":
            preset["margin_v"] = preset.get("gameplay_margin_v", 480)
        hook_margin_v = 240  # Top safe area

    limit_words = max_words if max_words is not None else preset.get("max_words_per_line", 5)
    gap_limit = gap_break if gap_break is not None else preset.get("gap_break_seconds", 0.6)

    groups: List[List[Tuple[float, float, str]]] = []
    cur: List[Tuple[float, float, str]] = []

    for w in words:
        start_t, end_t, text = w
        clean_text = text.strip()
        if not clean_text:
            continue

        if cur and (len(cur) >= limit_words or (start_t - cur[-1][1]) > gap_limit):
            groups.append(cur)
            cur = []
        cur.append((start_t, end_t, clean_text))

    if cur:
        groups.append(cur)

    lines: List[str] = []

    # Insert hook title banner if provided
    final_hook = hook_title or extract_hook_phrase(words)
    if final_hook:
        lines.append(
            f"Dialogue: 1,0:00:00.00,{fmt_ts(hook_duration)},Hook,,0,0,0,,{final_hook.upper()}"
        )

    for g in groups:
        # Build {\kf<cs>} tag for each word
        card_text = " ".join(
            r"{\kf%d}%s" % (max(1, int(round((e - s) * 100))), t.upper())
            for s, e, t in g
        )
        lines.append(f"Dialogue: 0,{fmt_ts(g[0][0])},{fmt_ts(g[-1][1])},Cap,,0,0,0,,{card_text}")

    content = build_ass_header(preset, hook_margin_v=hook_margin_v) + "\n".join(lines) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write(content)

    logger.info(f"Generated ASS subtitles with hook ('{final_hook}'): {output_path}")
    return os.path.abspath(output_path)


def escape_ffmpeg_ass_path(ass_path: str) -> str:
    """Safely escape an ASS file path for FFmpeg's -vf filtergraph on Windows.

    Windows paths often contain drive letters (C:) and spaces (e.g., 'CLIP AUTO').
    FFmpeg's filter parser treats colons as filter option delimiters and backslashes
    as escape characters. This function ensures FFmpeg parses the filename correctly.
    """
    abs_path = os.path.abspath(ass_path)
    # 1. Replace backslashes with forward slashes
    forward_path = abs_path.replace("\\", "/")
    # 2. Escape the drive colon (e.g. C: -> C\:)
    escaped_colon = forward_path.replace(":", r"\:")
    # 3. Escape single quotes if any
    escaped = escaped_colon.replace("'", r"\'")
    return escaped
