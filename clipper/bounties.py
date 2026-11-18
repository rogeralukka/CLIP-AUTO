"""clipper/bounties.py - Directory of active clipping bounties, reward pools, and account linking."""
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .campaigns import list_campaigns, load_campaign, save_campaign

logger = logging.getLogger("clipper.bounties")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
BOUNTIES_FILE = os.path.join(CONFIG_DIR, "bounties.json")

DEFAULT_BOUNTIES: List[Dict[str, Any]] = [
    {
        "id": "vyro_creator_network",
        "title": "MrBeast Gaming & Creator Distribution",
        "streamer": "Creator Network",
        "source": "vyro",
        "source_name": "Vyro",
        "payout_rate": "$3.00 CPM ($3.00 / 1K views)",
        "pool_size": "$50,000 Pool",
        "budget_remaining": "$34,200 remaining",
        "status": "HOT",
        "platform": "youtube",
        "allowed_platforms": ["tiktok", "youtube", "instagram"],
        "required_hashtags": ["#mrbeast", "#gaming", "#shorts"],
        "required_disclosure": "",
        "min_clip_length": 15,
        "max_clip_length": 60,
        "url": "https://vyro.com",
        "rules_note": "Cut engaging vertical gaming shorts. Payout is calculated per 1,000 verified views across TikTok, Shorts, and Reels.",
        "assigned_account_id": None,
    },
    {
        "id": "vyro_gta_streamers",
        "title": "GTA 6 & RP Streamer Clips",
        "streamer": "FiveM / GTA Streamers",
        "source": "vyro",
        "source_name": "Vyro",
        "payout_rate": "$2.50 CPM ($2.50 / 1K views)",
        "pool_size": "$25,000 Pool",
        "budget_remaining": "$18,500 remaining",
        "status": "ACTIVE",
        "platform": "twitch",
        "allowed_platforms": ["tiktok", "youtube", "instagram"],
        "required_hashtags": ["#gta6", "#gtarp", "#gamingclips"],
        "required_disclosure": "",
        "min_clip_length": 15,
        "max_clip_length": 50,
        "url": "https://vyro.com",
        "rules_note": "Funny RP encounters, police chases, and crazy stunts. Submit posted links on Vyro dashboard.",
        "assigned_account_id": None,
    },
    {
        "id": "discord_kai_cenat",
        "title": "Kai Cenat & AMP Monthly Clipper Rewards",
        "streamer": "Kai Cenat",
        "source": "discord",
        "source_name": "Streamer Discord",
        "payout_rate": "$10,000 Pool (Top 20 Leaderboard)",
        "pool_size": "$10,000 Monthly Pool",
        "budget_remaining": "Active this month",
        "status": "HOT",
        "platform": "twitch",
        "allowed_platforms": ["tiktok", "youtube", "instagram"],
        "required_hashtags": ["#kaicenat", "#amp", "#twitchclips"],
        "required_disclosure": "",
        "min_clip_length": 15,
        "max_clip_length": 60,
        "url": "https://discord.gg/kaicenat",
        "rules_note": "Monthly bounty pool for highest view counts. Submit links in #clipper-rewards channel on Discord.",
        "assigned_account_id": None,
    },
    {
        "id": "discord_adin_ross",
        "title": "Adin Ross Live Stream Clipper Pool",
        "streamer": "Adin Ross",
        "source": "discord",
        "source_name": "Streamer Discord",
        "payout_rate": "$15,000 Pool (PPV + Leaderboard)",
        "pool_size": "$15,000 Monthly Pool",
        "budget_remaining": "Active this month",
        "status": "HOT",
        "platform": "kick",
        "allowed_platforms": ["tiktok", "youtube", "instagram"],
        "required_hashtags": ["#adinross", "#kickstream", "#adinlive"],
        "required_disclosure": "",
        "min_clip_length": 15,
        "max_clip_length": 60,
        "url": "https://discord.gg/adin",
        "rules_note": "Post viral Kick highlights to TikTok/Shorts. Submit video links to the Adin Discord clipping bot.",
        "assigned_account_id": None,
    },
    {
        "id": "whop_kick_clippers",
        "title": "Kick Creator Rewards & Top Stream Highlights",
        "streamer": "Kick Top Streamers",
        "source": "whop",
        "source_name": "Whop Rewards",
        "payout_rate": "$3.50 CPM ($3.50 / 1K views)",
        "pool_size": "$20,000 Monthly Pool",
        "budget_remaining": "$14,000 remaining",
        "status": "ACTIVE",
        "platform": "kick",
        "allowed_platforms": ["tiktok", "youtube", "instagram", "facebook"],
        "required_hashtags": ["#kickstreaming", "#streamclips", "#viral"],
        "required_disclosure": "",
        "min_clip_length": 15,
        "max_clip_length": 60,
        "url": "https://whop.com",
        "rules_note": "Content Rewards bounty for Kick livestream clips driving external discovery. Payouts verified via Whop.",
        "assigned_account_id": None,
    },
    {
        "id": "cliptech_subathon",
        "title": "Subathon & Live Stream Bounties",
        "streamer": "Subathon Streamers",
        "source": "cliptech",
        "source_name": "Clip.tech",
        "payout_rate": "$2.80 CPM ($2.80 / 1K views)",
        "pool_size": "$12,000 Pool",
        "budget_remaining": "$7,500 remaining",
        "status": "ACTIVE",
        "platform": "twitch",
        "allowed_platforms": ["tiktok", "youtube", "instagram"],
        "required_hashtags": ["#subathon", "#liveclips"],
        "required_disclosure": "",
        "min_clip_length": 15,
        "max_clip_length": 45,
        "url": "https://clip.tech",
        "rules_note": "Browse briefs, opt in, and get paid weekly to PayPal based on verified views.",
        "assigned_account_id": None,
    },
    {
        "id": "discord_ishowspeed",
        "title": "IShowSpeed Daily Hype Moments",
        "streamer": "IShowSpeed",
        "source": "discord",
        "source_name": "Streamer Discord",
        "payout_rate": "$8,000 Pool (Monthly Payout)",
        "pool_size": "$8,000 Monthly Pool",
        "budget_remaining": "Active this month",
        "status": "ACTIVE",
        "platform": "youtube",
        "allowed_platforms": ["tiktok", "youtube", "instagram"],
        "required_hashtags": ["#ishowspeed", "#speedclips", "#viralshorts"],
        "required_disclosure": "",
        "min_clip_length": 15,
        "max_clip_length": 60,
        "url": "https://discord.gg/ishowspeed",
        "rules_note": "Speed stream reaction and gaming moments. Submit links in Speed Discord clippers channel.",
        "assigned_account_id": None,
    },
]


def load_bounties() -> List[Dict[str, Any]]:
    """Load bounties from disk and annotate with local campaign import status."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    bounties: List[Dict[str, Any]] = []

    if os.path.isfile(BOUNTIES_FILE):
        try:
            with open(BOUNTIES_FILE, "r", encoding="utf-8") as f:
                bounties = json.load(f) or []
        except Exception as e:
            logger.warning(f"Error loading {BOUNTIES_FILE}: {e}")

    if not bounties:
        bounties = list(DEFAULT_BOUNTIES)
        save_bounties(bounties)

    # Check imported campaigns
    local_campaigns = {c["id"]: c for c in list_campaigns()}
    for b in bounties:
        bid = b.get("id")
        camp = local_campaigns.get(bid)
        if camp:
            b["is_imported"] = True
            b["campaign_id"] = bid
            if not b.get("assigned_account_id") and camp.get("assigned_account_id"):
                b["assigned_account_id"] = camp.get("assigned_account_id")
        else:
            b["is_imported"] = False
            b["campaign_id"] = None

    return bounties


def save_bounties(bounties: List[Dict[str, Any]]) -> None:
    """Save bounties list to disk."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(BOUNTIES_FILE, "w", encoding="utf-8") as f:
        json.dump(bounties, f, indent=2)


def get_bounty(bounty_id: str) -> Optional[Dict[str, Any]]:
    """Get a single bounty by ID."""
    bounties = load_bounties()
    for b in bounties:
        if b.get("id") == bounty_id:
            return b
    return None


def upsert_bounty(bounty_data: Dict[str, Any]) -> str:
    """Add or update a bounty in the registry."""
    bounties = load_bounties()
    bid = bounty_data.get("id") or f"bounty_{int(time.time())}"

    existing_idx = next((i for i, b in enumerate(bounties) if b.get("id") == bid), -1)
    entry = dict(bounty_data)
    entry["id"] = bid
    entry.setdefault("source", "custom")
    entry.setdefault("source_name", "Custom / Discord")
    entry.setdefault("status", "ACTIVE")

    if existing_idx >= 0:
        bounties[existing_idx].update(entry)
    else:
        bounties.insert(0, entry)

    save_bounties(bounties)
    return bid


def delete_bounty(bounty_id: str) -> bool:
    """Delete a bounty from the registry."""
    bounties = load_bounties()
    orig_len = len(bounties)
    bounties = [b for b in bounties if b.get("id") != bounty_id]
    if len(bounties) != orig_len:
        save_bounties(bounties)
        return True
    return False


def link_bounty_account(bounty_id: str, account_id: Optional[str]) -> bool:
    """Link a specific creator account group to a bounty (and its campaign)."""
    bounties = load_bounties()
    updated = False
    for b in bounties:
        if b.get("id") == bounty_id:
            b["assigned_account_id"] = account_id or None
            updated = True
            break

    if updated:
        save_bounties(bounties)

    # Also update campaign file if already imported
    try:
        camp = load_campaign(bounty_id)
        if camp:
            camp["assigned_account_id"] = account_id or None
            save_campaign(camp, bounty_id)
    except FileNotFoundError:
        pass

    return updated


def import_bounty_as_campaign(bounty_id: str, account_id: Optional[str] = None) -> Dict[str, Any]:
    """Import a bounty directly as a local Campaign preset in CLIP AUTO."""
    bounty = get_bounty(bounty_id)
    if not bounty:
        raise ValueError(f"Bounty '{bounty_id}' not found.")

    chosen_account = account_id or bounty.get("assigned_account_id")

    camp_data = {
        "name": bounty.get("title", ""),
        "streamer": bounty.get("streamer", ""),
        "platform": bounty.get("platform", "twitch"),
        "min_clip_length": bounty.get("min_clip_length", 15),
        "max_clip_length": bounty.get("max_clip_length", 60),
        "preset": "classic_yellow",
        "required_hashtags": bounty.get("required_hashtags", []),
        "required_disclosure": bounty.get("required_disclosure", ""),
        "allowed_upload_platforms": bounty.get("allowed_platforms", []),
        "rules_note": f"Bounty: {bounty.get('title')} ({bounty.get('source_name')})\nPayout: {bounty.get('payout_rate')}\nLink: {bounty.get('url')}\n\n{bounty.get('rules_note', '')}",
        "assigned_account_id": chosen_account,
        "bounty_url": bounty.get("url", ""),
        "payout": bounty.get("payout_rate", ""),
        "bounty_source": bounty.get("source", ""),
    }

    cid = save_campaign(camp_data, campaign_id=bounty_id)
    link_bounty_account(bounty_id, chosen_account)

    return load_campaign(cid)
