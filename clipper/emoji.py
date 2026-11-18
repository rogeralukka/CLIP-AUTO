"""Emoji reactions for captions.

Plain-text emoji in subtitles break on many Windows ffmpeg builds (no colour-emoji font), so
emoji are small pictures (assets/emoji/*.png) that Studio lays over the clip while the matching
words are on screen. Picked only from words actually said; nothing is invented.
"""
import os
import re
from typing import Dict, List, Optional, Tuple

ASSET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "emoji")

# keyword (lower-case, matched on the whole word or its start for the "*" entries) -> emoji
_RULES: List[Tuple[str, str]] = [
    ("haha hahaha lol lmao lmfao funny joke laughing hilarious rofl", "😂"),
    ("omg wow whoa woah damn unbelievable insane crazy wtf", "😱"),
    ("fire lit hot banger goated", "🔥"),
    ("money dollar dollars million billion rich cash paid", "💰"),
    ("dead die died dying kill killed death", "💀"),
    ("love loved heart cute", "❤️"),
    ("gun shoot shot shooting", "🔫"),
    ("car cars drive driving race racing crash crashed", "🚗"),
    ("win won winning victory champion gg", "🏆"),
    ("cry crying sad lost lose loser fail failed", "😭"),
    ("scared scary ghost horror afraid terrified", "😨"),
    ("angry mad hate furious rage", "😡"),
    ("police cops cop arrested jail", "🚓"),
    ("fight fighting punch", "🥊"),
    ("hello hey", "👋"),
    ("look looking eyes", "👀"),
    ("think thinking maybe wonder idea", "🤔"),
    ("clap respect legend", "👏"),
    ("sleep tired sleepy", "😴"),
    ("food burger pizza eat hungry", "🍔"),
    ("run running chase", "🏃"),
    ("hundred real facts truth", "💯"),
    ("help", "🆘"),
    ("boom explode explosion bomb", "💥"),
    ("stop wait", "✋"),
    ("lets let's go gonna", "🚀"),
    ("gta", "🎮"),
    ("sorry please", "🥺"),
    ("good great nice amazing awesome best", "👍"),
    ("bad worst terrible trash", "👎"),
    ("what why how", "😳"),
]

_WORD_TO_EMOJI: Dict[str, str] = {}
for _words, _emo in _RULES:
    for _w in _words.split():
        _WORD_TO_EMOJI.setdefault(_w, _emo)

EMOJI_SET = sorted({e for _, e in _RULES})
# the weak "question" emoji only when nothing stronger fits, and not too often
_WEAK = {"😳", "👍", "👎", "👋", "🥺", "🚀", "🎮"}


def emoji_filename(ch: str) -> str:
    return "-".join(f"{ord(c):x}" for c in ch if ord(c) != 0xFE0F) + ".png"


def emoji_path(ch: str) -> Optional[str]:
    p = os.path.join(ASSET_DIR, emoji_filename(ch))
    return p if os.path.isfile(p) else None


def pick_emoji_for_words(words: List[str]) -> Optional[str]:
    """Strongest matching emoji for a few words, or None."""
    found: List[str] = []
    for w in words:
        key = re.sub(r"[^\w']", "", w.lower())
        emo = _WORD_TO_EMOJI.get(key)
        if emo:
            found.append(emo)
    if not found:
        return None
    strong = [e for e in found if e not in _WEAK]
    return (strong or found)[0]


def plan_emoji_events(cards: List[List[Tuple[float, float, str]]], min_gap: float = 1.6,
                      max_events: int = 14, max_weak: int = 3) -> List[Tuple[float, float, str, str]]:
    """Choose which caption cards get an emoji. Returns [(start, end, emoji, png_path)].

    At most one emoji per `min_gap` seconds, never the same one twice in a row, and the
    weak/generic ones are limited so the video is not littered with them."""
    events: List[Tuple[float, float, str, str]] = []
    last_end, last_emo, weak = -99.0, None, 0
    for card in cards:
        if len(events) >= max_events:
            break
        start, end = card[0][0], card[-1][1]
        if start - last_end < min_gap:
            continue
        emo = pick_emoji_for_words([w[2] for w in card])
        if not emo or emo == last_emo:
            continue
        path = emoji_path(emo)
        if not path:
            continue
        if emo in _WEAK:
            if weak >= max_weak:
                continue
            weak += 1
        # stay visible long enough to register, but never past the next card's start much
        end = max(end, start + 0.9)
        events.append((round(start, 2), round(end, 2), emo, path))
        last_end, last_emo = end, emo
    return events
