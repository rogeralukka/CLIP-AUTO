"""Campaign management, persistent CRUD, and risk/spoiler word detection."""
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("clipper.campaigns")

DEFAULT_RISK_WORDS = [
    "leak", "leaked", "spoiler", "spoilers", "banned", "dmca", "copyright",
    "slur", "n-word", "scam", "cheat", "hack", "dox", "doxxed", "address"
]


def load_campaign(yaml_path_or_name: str) -> Dict[str, Any]:
    """Load a campaign YAML file by name or path."""
    target_path = yaml_path_or_name
    if not os.path.isfile(target_path):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidate = os.path.join(base_dir, "campaigns", yaml_path_or_name)
        if not candidate.endswith(".yaml") and not candidate.endswith(".yml"):
            candidate += ".yaml"
        if os.path.isfile(candidate):
            target_path = candidate
        else:
            raise FileNotFoundError(f"Campaign file not found: {yaml_path_or_name}")

    with open(target_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    campaign = {
        "id": os.path.splitext(os.path.basename(target_path))[0],
        "name": data.get("name", ""),
        "streamer": data.get("streamer", ""),
        "platform": data.get("platform", ""),
        "min_clip_length": data.get("min_clip_length", 15),
        "max_clip_length": data.get("max_clip_length", 180),
        "preset": data.get("preset", "classic_yellow"),
        "overlay": data.get("overlay", None),
        "required_hashtags": data.get("required_hashtags", []),
        "required_disclosure": data.get("required_disclosure", ""),
        "allowed_upload_platforms": data.get("allowed_upload_platforms", []),
        "rules_note": data.get("rules_note", ""),
        "risk_words": data.get("risk_words", DEFAULT_RISK_WORDS),
        "assigned_account_id": data.get("assigned_account_id", None),
        "bounty_url": data.get("bounty_url", ""),
        "payout": data.get("payout", ""),
        "bounty_source": data.get("bounty_source", ""),
        "file_path": os.path.abspath(target_path),
    }
    return campaign


def list_campaigns(campaigns_dir: str = "campaigns") -> List[Dict[str, Any]]:
    """List all available campaigns in the campaigns directory."""
    if not os.path.isdir(campaigns_dir):
        return []
    campaigns = []
    for f in os.listdir(campaigns_dir):
        if f.endswith(".yaml") or f.endswith(".yml"):
            try:
                full_path = os.path.join(campaigns_dir, f)
                c = load_campaign(full_path)
                campaigns.append(c)
            except Exception as e:
                logger.warning(f"Failed to load campaign file {f}: {e}")
    return campaigns


def save_campaign(campaign_data: Dict[str, Any], campaign_id: Optional[str] = None) -> str:
    """Save or update a campaign YAML file on disk (Requirement B)."""
    os.makedirs("campaigns", exist_ok=True)
    cid = campaign_id
    if not cid:
        raw_name = campaign_data.get("name", "").lower().strip()
        cid = re.sub(r'[^a-z0-9_-]', '_', raw_name)
    if not cid or cid == "_":
        cid = f"campaign_{int(time.time())}"

    file_path = os.path.join("campaigns", f"{cid}.yaml")
    clean_data = dict(campaign_data)
    clean_data.pop("file_path", None)
    clean_data.pop("id", None)

    with open(file_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(clean_data, f, sort_keys=False)

    logger.info(f"Saved campaign '{cid}' to {file_path}")
    return cid


def delete_campaign(campaign_id: str) -> bool:
    """Permanently delete a campaign YAML file (Requirement B)."""
    for ext in (".yaml", ".yml"):
        file_path = os.path.join("campaigns", f"{campaign_id}{ext}")
        if os.path.isfile(file_path):
            os.remove(file_path)
            logger.info(f"Deleted campaign file: {file_path}")
            return True
    return False


def screen_transcript_for_risks(
    transcript_text: str,
    custom_risk_words: Optional[List[str]] = None
) -> Tuple[bool, List[str]]:
    """Check transcript for spoiler, prohibited, or brand risk words."""
    if not transcript_text:
        return False, []

    words_to_check = set(w.lower() for w in DEFAULT_RISK_WORDS)
    if custom_risk_words:
        words_to_check.update(w.lower() for w in custom_risk_words)

    matched = []
    lower_transcript = transcript_text.lower()
    for rw in words_to_check:
        pattern = r"\b" + re.escape(rw) + r"\b"
        if re.search(pattern, lower_transcript):
            matched.append(rw)

    return (len(matched) > 0), sorted(matched)
