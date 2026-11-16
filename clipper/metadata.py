"""Metadata generator strictly built from real transcript text and user campaign configurations.

Adheres strictly to the Golden Rule:
No fake placeholders, no random templates. If a title cannot be derived from real transcript words,
leave it empty.
"""
import re
from typing import Any, Dict, List, Optional


def extract_transcript_hook(transcript: str, max_words: int = 8) -> str:
    """Extract an authentic hook quote directly from the clip's actual transcript."""
    if not transcript or not transcript.strip():
        return ""
    clean = re.sub(r"[^\w\s\'-]", "", transcript).strip()
    words = clean.split()
    if not words:
        return ""
    snippet = " ".join(words[:max_words]).capitalize()
    return f'"{snippet}"'


def generate_clip_metadata(
    clip_info: Dict[str, Any],
    campaign: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Generate metadata strictly from real transcript words and user-configured campaign fields."""
    streamer = (campaign.get("streamer") or "").strip() if campaign else ""
    transcript = clip_info.get("transcript", "").strip()

    # 1. Title: ONLY from real transcript words. Never fake clickbait templates.
    hook = extract_transcript_hook(transcript)
    if hook:
        if streamer:
            title = f"{streamer} - {hook}"
        else:
            title = hook
    else:
        # If no transcript exists or no words were transcribed, leave title empty.
        title = ""

    # 2. Hashtags: ONLY user-configured hashtags from the chosen campaign.
    hashtags_list = []
    if campaign and campaign.get("required_hashtags"):
        for tag in campaign["required_hashtags"]:
            clean_tag = tag.strip()
            if clean_tag:
                t = clean_tag if clean_tag.startswith("#") else f"#{clean_tag}"
                if t not in hashtags_list:
                    hashtags_list.append(t)
    hashtags_str = " ".join(hashtags_list)

    # 3. Description: real transcript snippet, user disclosure, streamer, and user hashtags.
    desc_lines = []
    if transcript:
        desc_lines.append(f'"{transcript}"')
        desc_lines.append("")

    if campaign:
        disclosure = (campaign.get("required_disclosure") or "").strip()
        if disclosure:
            desc_lines.append(f"Disclosure: {disclosure}")

        if streamer:
            desc_lines.append(f"Streamer: {streamer}")

    if hashtags_str:
        desc_lines.append(hashtags_str)

    return {
        "title": title,
        "description": "\n".join(desc_lines).strip(),
        "hashtags": hashtags_str,
    }
