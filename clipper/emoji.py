"""Emoji reactions for captions.

Plain-text emoji in subtitles break on many Windows ffmpeg builds (no colour-emoji font), so
emoji are small pictures (assets/emoji/*.png, Noto Color Emoji = the Android set, SIL OFL)
that Studio lays over the clip while the matching words are on screen. Picked only from words
actually said; nothing is invented.
"""
import os
import re
from typing import Dict, List, Optional, Tuple

ASSET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "emoji")

# words (lower-case; "a_b" = the two words in a row) -> emoji. First rule wins for a word.
_RULES: List[Tuple[str, str]] = [
    ("laugh haha hahaha hahahaha lol lmao lmfao rofl funny hilarious joke jokes joking laughing laughed giggle comedy", "😂"),
    ("dying cracking crack cracked", "🤣"),
    ("omg wow whoa woah damn unbelievable insane crazy wtf shocked shocking shock no_way jesus holy", "😱"),
    ("mindblown mind_blown unreal impossible", "🤯"),
    ("fire lit banger goated heat hot flames burning burn", "🔥"),
    ("money dollar dollars cash rich million millions billion billions paid pay payment cheque wealthy", "💰"),
    ("bucks price expensive cost costs", "💸"),
    ("dead die died dying kill killed killing death murder murdered rip skull", "💀"),
    ("love loved loving heart cute adorable bae girlfriend boyfriend wife husband date", "❤️"),
    ("kiss kissed kissing", "😘"),
    ("gun guns shoot shot shooting rifle pistol sniper", "🔫"),
    ("car cars drive driving driver race racing crash crashed vehicle truck", "🚗"),
    ("bike motorcycle", "🏍️"),
    ("plane flight flying fly airplane", "✈️"),
    ("win won winning victory champion champions gg trophy", "🏆"),
    ("medal", "🥇"),
    ("cry crying cried sad tears sadly sob", "😭"),
    ("lost lose losing loser fail failed failing fell fall falling", "📉"),
    ("scared scary ghost horror afraid terrified spooky creepy nightmare", "😨"),
    ("angry mad hate hated furious rage pissed annoyed annoying", "😡"),
    ("cursing curse swear swearing", "🤬"),
    ("police cops cop arrested arrest jail prison locked", "🚓"),
    ("fight fighting fought punch punched boxing", "🥊"),
    ("hello hey hi welcome greet greeting wave", "👋"),
    ("look looking looked eyes stare staring watching sus suspicious", "👀"),
    ("think thinking thought maybe wonder wondering idea hmm", "🤔"),
    ("clap clapping applause respect legend legendary bravo", "👏"),
    ("sleep sleeping tired sleepy exhausted bed night", "😴"),
    ("food burger pizza eat eating ate hungry fries chicken", "🍔"),
    ("drink drinking water thirsty", "🥤"),
    ("run running ran chase chased chasing escape escaping", "🏃"),
    ("hundred real facts truth honest honestly fr", "💯"),
    ("help save rescue", "🆘"),
    ("boom explode explosion bomb blow blew blast exploded", "💥"),
    ("stop wait hold halt", "✋"),
    ("lets let's go gonna rocket launch hype", "🚀"),
    ("gta game gaming gamer roblox minecraft fortnite controller play played playing", "🎮"),
    ("sorry please apologize apology beg begging", "🥺"),
    ("good great nice amazing awesome best perfect excellent solid cool sick", "👍"),
    ("bad worst terrible trash awful horrible garbage", "👎"),
    ("what why how huh confused confusing", "😳"),
    ("question", "❓"),
    ("yes yeah yep sure okay ok alright agreed correct right", "✅"),
    ("no nope nah never wrong incorrect", "❌"),
    ("smile smiling happy glad joy fun enjoy enjoyed", "😄"),
    ("excited exciting excitement thrilled", "🤩"),
    ("nervous nervously anxious worried worry sweating sweat stress stressed", "😰"),
    ("embarrassed embarrassing awkward cringe shy", "😬"),
    ("bored boring", "🥱"),
    ("sick ill vomit puke gross disgusting nasty ew eww", "🤢"),
    ("cold freezing freeze ice snow", "🥶"),
    ("summer sun sunny", "☀️"),
    ("rain raining storm thunder lightning", "⛈️"),
    ("music song sing singing singer rap rapper", "🎵"),
    ("dance dancing dancer dances", "💃"),
    ("phone call calling text texting dm message", "📱"),
    ("camera video record recording film filming stream streaming live", "📹"),
    ("chat chatting talk talking speak speaking", "💬"),
    ("shut quiet silence silent shh", "🤫"),
    ("secret secrets hide hiding hidden", "🤐"),
    ("lie lying liar lied fake cap", "🧢"),
    ("king queen boss goat", "👑"),
    ("strong strength muscle muscles gym workout lift lifting", "💪"),
    ("fast speed quick quickly speedy flash", "⚡"),
    ("slow slowly", "🐢"),
    ("dog dogs puppy", "🐶"),
    ("cat cats kitten", "🐱"),
    ("monkey ape", "🐵"),
    ("bear", "🐻"),
    ("snake", "🐍"),
    ("shark", "🦈"),
    ("pig", "🐷"),
    ("horse", "🐴"),
    ("bird", "🐦"),
    ("fish fishing", "🐟"),
    ("tree trees forest", "🌳"),
    ("house home room", "🏠"),
    ("school class teacher homework exam test", "📚"),
    ("work working job office", "💼"),
    ("computer pc laptop code coding", "💻"),
    ("clock minute minutes second seconds hour late", "⏰"),
    ("birthday party celebrate celebration congrats congratulations", "🎉"),
    ("gift present", "🎁"),
    ("shirt clothes outfit hoodie jacket", "👕"),
    ("shoes sneakers", "👟"),
    ("hat", "🎩"),
    ("glasses sunglasses", "😎"),
    ("hair haircut hairline bald", "💇"),
    ("teeth tooth", "🦷"),
    ("pray prayer god please_god bless", "🙏"),
    ("angel", "😇"),
    ("devil evil demon", "😈"),
    ("alien aliens space ufo", "👽"),
    ("robot ai", "🤖"),
    ("clown", "🤡"),
    ("poop shit crap", "💩"),
    ("smell stink stinks", "👃"),
    ("listen listening hear heard", "👂"),
    ("brain smart genius intelligent", "🧠"),
    ("dumb stupid idiot", "🤪"),
    ("wild chaos chaotic", "🤪"),
    ("star stars famous", "⭐"),
    ("diamond", "💎"),
    ("lock", "🔒"),
    ("tv television", "📺"),
    ("ball football soccer basketball", "⚽"),
    ("tower", "🗼"),
    ("map", "🗺️"),
    ("world earth", "🌍"),
    ("flag", "🚩"),
    ("warning danger careful", "⚠️"),
    ("sword knife stab", "🔪"),
    ("bow", "🏹"),
    ("shield protect protection", "🛡️"),
    ("coffee tea", "☕"),
    ("beer", "🍺"),
    ("cake", "🎂"),
    ("ice_cream", "🍦"),
    ("apple", "🍎"),
    ("banana", "🍌"),
    ("egg", "🥚"),
    ("rice", "🍚"),
    ("level levels rank ranked", "📈"),
    ("score points", "🎯"),
    ("vote voting", "🗳️"),
    ("news", "📰"),
    ("gold golden", "🥇"),
    ("silver", "🥈"),
    ("bronze", "🥉"),
    ("no_way cant_believe oh_my_god holy_shit what_the_hell", "😱"),
    ("lets_go let's_go", "🚀"),
    ("thank_you thanks thank", "🙏"),
    ("good_job well_done", "👏"),
    ("bro_what", "😳"),
]

_WORD_TO_EMOJI: Dict[str, str] = {}
for _words, _emo in _RULES:
    for _w in _words.split():
        _WORD_TO_EMOJI.setdefault(_w.replace("'", ""), _emo)

EMOJI_SET = sorted({e for _, e in _RULES})
# generic reactions: allowed, but limited so the video is not littered with them
_WEAK = {"😳", "👍", "👎", "👋", "🥺", "🚀", "🎮", "✅", "❌", "❓", "🤔", "👀", "💬", "🙏", "📈", "📉", "🎯", "🏠", "⏰", "📱", "📹"}


def emoji_filename(ch: str) -> str:
    return "-".join(f"{ord(c):x}" for c in ch if ord(c) != 0xFE0F) + ".png"


def emoji_path(ch: str) -> Optional[str]:
    p = os.path.join(ASSET_DIR, emoji_filename(ch))
    return p if os.path.isfile(p) else None


def _clean(w: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", w.lower().replace("'", ""))


def _lookup(word: str) -> Optional[str]:
    if word in _WORD_TO_EMOJI:
        return _WORD_TO_EMOJI[word]
    for suf in ("ing", "ed", "es", "s", "ly"):
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            base = word[: -len(suf)]
            for cand in (base, base + "e"):
                if cand in _WORD_TO_EMOJI:
                    return _WORD_TO_EMOJI[cand]
    return None


def pick_emoji_for_words(words: List[str]) -> Optional[str]:
    """Strongest matching emoji for a few words, or None."""
    toks = [_clean(w) for w in words]
    toks = [t for t in toks if t]
    found: List[str] = []
    for n in (3, 2):        # phrases first ("no way", "oh my god")
        for i in range(len(toks) - n + 1):
            emo = _WORD_TO_EMOJI.get("_".join(toks[i:i + n]))
            if emo:
                found.append(emo)
    for t in toks:
        emo = _lookup(t)
        if emo:
            found.append(emo)
    if not found:
        return None
    strong = [e for e in found if e not in _WEAK]
    return (strong or found)[0]


def plan_emoji_events(cards: List[List[Tuple[float, float, str]]], min_gap: float = 0.9,
                      max_events: int = 40, max_weak: int = 5, repeat_gap: float = 4.0
                      ) -> List[Tuple[float, float, str, str]]:
    """Choose which caption cards get an emoji. Returns [(start, end, emoji, png_path)].

    At most one emoji every `min_gap` seconds, the same emoji is not repeated within
    `repeat_gap` seconds, and the generic ones are limited."""
    events: List[Tuple[float, float, str, str]] = []
    last_end = -99.0
    last_seen: Dict[str, float] = {}
    weak = 0
    for card in cards:
        if len(events) >= max_events:
            break
        start, end = card[0][0], card[-1][1]
        if start < last_end + min_gap - 0.9:     # previous emoji still on screen
            continue
        emo = pick_emoji_for_words([w[2] for w in card])
        if not emo or start - last_seen.get(emo, -99.0) < repeat_gap:
            continue
        path = emoji_path(emo)
        if not path:
            continue
        if emo in _WEAK:
            if weak >= max_weak:
                continue
            weak += 1
        end = max(end, start + 0.9)
        events.append((round(start, 2), round(end, 2), emo, path))
        last_end = end
        last_seen[emo] = start
    return events
