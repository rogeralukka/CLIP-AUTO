"""clipper/social.py - Social accounts manager, multi-platform dispatcher, and native server-side scheduler.

Enables creators to manage 3-4 accounts (each linked to YouTube Shorts, Instagram Reels,
TikTok, and Facebook Reels), enforces a strict daily limit (3-4 clips/day per account),
ensures clips are unique per account, and uploads directly to platforms with native
server-side scheduling so videos publish automatically even when the laptop is shut down.
"""
import datetime
import glob
import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("clipper.social")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
TOKENS_DIR = os.path.join(CONFIG_DIR, "tokens")
os.makedirs(TOKENS_DIR, exist_ok=True)

ACCOUNTS_FILE = os.path.join(CONFIG_DIR, "social_accounts.yaml")
SCHEDULE_QUEUE_FILE = os.path.join(CONFIG_DIR, "scheduled_posts.json")


# ---------------------------------------------------------------------------
# Account Management
# ---------------------------------------------------------------------------

def load_accounts() -> Dict[str, Dict[str, Any]]:
    """Load all configured social accounts."""
    if not os.path.isfile(ACCOUNTS_FILE):
        return {}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data.get("accounts", {})
    except Exception as e:
        logger.error(f"Error loading {ACCOUNTS_FILE}: {e}")
        return {}


def save_accounts(accounts: Dict[str, Dict[str, Any]]) -> None:
    """Save all configured social accounts."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump({"accounts": accounts}, f, sort_keys=False)


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific account by ID."""
    accounts = load_accounts()
    return accounts.get(account_id)


def upsert_account(account_id: str, data: Dict[str, Any]) -> str:
    """Create or update an account profile."""
    accounts = load_accounts()
    aid = account_id.strip() if account_id else f"acc_{int(time.time())}"

    existing = accounts.get(aid, {})
    existing.update(data)
    existing["id"] = aid
    existing.setdefault("name", f"Account {aid}")
    existing.setdefault("max_clips_per_day", 4)
    existing.setdefault("schedule_slots", ["11:00", "15:00", "19:00", "22:00"])
    existing.setdefault("platforms", {
        "youtube": {"enabled": False, "channel_name": ""},
        "instagram": {"enabled": False, "account_name": ""},
        "facebook": {"enabled": False, "page_name": ""},
        "tiktok": {"enabled": False, "account_name": ""},
    })
    accounts[aid] = existing
    save_accounts(accounts)
    return aid


def delete_account(account_id: str) -> bool:
    """Delete an account profile."""
    accounts = load_accounts()
    if account_id in accounts:
        del accounts[account_id]
        save_accounts(accounts)
        return True
    return False


# ---------------------------------------------------------------------------
# Schedule Queue & Tracking
# ---------------------------------------------------------------------------

def load_schedule_queue() -> List[Dict[str, Any]]:
    """Load the scheduled posts queue."""
    if not os.path.isfile(SCHEDULE_QUEUE_FILE):
        return []
    try:
        with open(SCHEDULE_QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception as e:
        logger.error(f"Error reading {SCHEDULE_QUEUE_FILE}: {e}")
        return []


def save_schedule_queue(queue: List[Dict[str, Any]]) -> None:
    """Save the scheduled posts queue."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SCHEDULE_QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)


def get_daily_scheduled_count(account_id: str, target_date_str: Optional[str] = None) -> int:
    """Get count of clips scheduled or published for an account on a given date (YYYY-MM-DD)."""
    if not target_date_str:
        target_date_str = datetime.date.today().isoformat()
    queue = load_schedule_queue()
    count = 0
    for post in queue:
        if post.get("account_id") == account_id:
            sched = post.get("scheduled_time") or ""
            if sched.startswith(target_date_str):
                count += 1
    return count


def get_assigned_clip_account(clip_path: str) -> Optional[str]:
    """Check if a clip has already been assigned/scheduled to any account."""
    norm_clip = os.path.normpath(os.path.abspath(clip_path)).lower()
    queue = load_schedule_queue()
    for post in queue:
        p_clip = os.path.normpath(os.path.abspath(post.get("clip_path") or "")).lower()
        if p_clip == norm_clip:
            return post.get("account_id")
    return None


def delete_schedule_entry(post_id: str) -> bool:
    """Remove a post from the scheduled posts queue."""
    queue = load_schedule_queue()
    orig_len = len(queue)
    queue = [p for p in queue if p.get("id") != post_id]
    if len(queue) != orig_len:
        save_schedule_queue(queue)
        return True
    return False


def calculate_next_slot(account_id: str) -> Tuple[str, str]:
    """Calculate the next available (date_str, time_str) for an account based on daily limits."""
    acc = get_account(account_id) or {}
    max_per_day = int(acc.get("max_clips_per_day", 4))
    slots = acc.get("schedule_slots") or ["11:00", "15:00", "19:00", "22:00"]

    now = datetime.datetime.now()
    today_str = now.date().isoformat()

    # Check today's open slots
    queue = load_schedule_queue()
    used_slots_today = set()
    for post in queue:
        if post.get("account_id") == account_id:
            st = post.get("scheduled_time") or ""
            if st.startswith(today_str):
                # Extract time part HH:MM
                parts = st.split("T")
                if len(parts) > 1:
                    used_slots_today.add(parts[1][:5])

    for s in slots:
        slot_hour, slot_min = map(int, s.split(":"))
        slot_dt = now.replace(hour=slot_hour, minute=slot_min, second=0, microsecond=0)
        # At least 15 minutes into the future
        if slot_dt > now + datetime.timedelta(minutes=15) and s not in used_slots_today:
            if len(used_slots_today) < max_per_day:
                return today_str, s

    # Roll over to tomorrow
    tomorrow = now.date() + datetime.timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()
    return tomorrow_str, slots[0] if slots else "12:00"


# ---------------------------------------------------------------------------
# Platform Connectors
# ---------------------------------------------------------------------------

def get_youtube_service(account_id: str):
    """Build and return an authorized YouTube Resource service object."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
    token_file = os.path.join(TOKENS_DIR, f"yt_token_{account_id}.json")
    client_secret_file = os.path.join(CONFIG_DIR, "youtube_client_secret.json")

    creds = None
    if os.path.isfile(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except Exception as e:
            logger.warning(f"Could not load token file {token_file}: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        else:
            if not os.path.isfile(client_secret_file):
                raise FileNotFoundError(
                    f"Google OAuth client secrets not found at {client_secret_file}. "
                    "Place your downloaded client_secret.json in config/youtube_client_secret.json"
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_file, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(
    account_id: str,
    video_path: str,
    title: str,
    description: str,
    tags: Optional[List[str]] = None,
    schedule_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload a short video directly to YouTube with native scheduling."""
    from googleapiclient.http import MediaFileUpload

    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    youtube = get_youtube_service(account_id)

    # YouTube Shorts requirements: include #Shorts in title or description
    if "#Shorts" not in title and "#shorts" not in title:
        title = f"{title[:90]} #Shorts"

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags or ["Shorts", "Gaming"],
            "categoryId": "20",  # Gaming
        },
        "status": {
            "selfDeclaredMadeForKids": False,
        }
    }

    if schedule_iso:
        # Native Server-Side Scheduling
        # Convert local ISO timestamp to UTC ISO 8601 with trailing 'Z'
        dt = datetime.datetime.fromisoformat(schedule_iso)
        utc_dt = dt.astimezone(datetime.timezone.utc)
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(f"Scheduling YouTube video for UTC {body['status']['publishAt']}...")
    else:
        body["status"]["privacyStatus"] = "public"

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info(f"YouTube upload progress: {int(status.progress() * 100)}%")

    video_id = response.get("id")
    video_url = f"https://youtu.be/{video_id}"
    logger.info(f"YouTube upload succeeded! Video ID: {video_id} ({video_url})")
    return {
        "platform": "youtube",
        "status": "SCHEDULED" if schedule_iso else "PUBLISHED",
        "video_id": video_id,
        "url": video_url,
    }


def upload_to_meta_reels(
    account_id: str,
    platform: str,  # "instagram" or "facebook"
    video_path: str,
    caption: str,
    schedule_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload and natively schedule a Reel to Instagram or Facebook via Meta Graph API."""
    import requests

    acc = get_account(account_id) or {}
    p_config = (acc.get("platforms") or {}).get(platform) or {}
    token = p_config.get("access_token")
    target_id = p_config.get("ig_user_id") if platform == "instagram" else p_config.get("page_id")

    if not token or not target_id:
        return {
            "platform": platform,
            "status": "SKIPPED",
            "reason": f"No {platform} access token or ID configured for account {account_id}."
        }

    # If scheduling, convert ISO to unix epoch
    publish_timestamp = None
    if schedule_iso:
        dt = datetime.datetime.fromisoformat(schedule_iso)
        publish_timestamp = int(dt.timestamp())

    # Meta Graph API Reels Container creation
    base_url = f"https://graph.facebook.com/v19.0/{target_id}"
    logger.info(f"Initiating {platform.upper()} Reel upload to target {target_id}...")

    # For local files, Meta supports resumable upload sessions
    session_url = f"{base_url}/video_reels"
    init_params = {
        "access_token": token,
        "upload_phase": "start",
    }
    r = requests.post(session_url, params=init_params, timeout=15)
    r_data = r.json()
    video_id = r_data.get("video_id")
    upload_url = r_data.get("upload_url")

    if not upload_url or not video_id:
        return {
            "platform": platform,
            "status": "FAILED",
            "error": f"Meta Graph API error: {r_data}"
        }

    # Upload binary chunks
    with open(video_path, "rb") as vf:
        headers = {
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(os.path.getsize(video_path)),
        }
        ur = requests.post(upload_url, headers=headers, data=vf, timeout=120)

    # Finish & Schedule phase
    finish_params = {
        "access_token": token,
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "SCHEDULED" if publish_timestamp else "PUBLISHED",
        "description": caption,
    }
    if publish_timestamp:
        finish_params["scheduled_publish_time"] = publish_timestamp

    fr = requests.post(session_url, params=finish_params, timeout=15)
    fr_data = fr.json()

    return {
        "platform": platform,
        "status": "SCHEDULED" if publish_timestamp else "PUBLISHED",
        "video_id": video_id,
        "meta": fr_data,
    }


def upload_to_tiktok(
    account_id: str,
    video_path: str,
    title: str,
    schedule_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload to TikTok via TikTok Content Posting API or local creator session."""
    acc = get_account(account_id) or {}
    t_config = (acc.get("platforms") or {}).get("tiktok") or {}
    token = t_config.get("access_token")

    if not token:
        return {
            "platform": "tiktok",
            "status": "SKIPPED",
            "reason": f"No TikTok access token configured for account {account_id}."
        }

    # TikTok Content Posting API
    # https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
    import requests
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body: Dict[str, Any] = {
        "post_info": {
            "title": title[:150],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_duet": False,
            "disable_stitch": False,
            "disable_comment": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": os.path.getsize(video_path),
            "chunk_size": os.path.getsize(video_path),
            "total_chunk_count": 1,
        }
    }
    if schedule_iso:
        dt = datetime.datetime.fromisoformat(schedule_iso)
        body["post_info"]["schedule_time"] = int(dt.timestamp())

    init_res = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers=headers,
        json=body,
        timeout=15,
    )
    init_data = init_res.json()
    publish_id = (init_data.get("data") or {}).get("publish_id")
    upload_url = (init_data.get("data") or {}).get("upload_url")

    if not upload_url:
        return {
            "platform": "tiktok",
            "status": "FAILED",
            "error": f"TikTok init error: {init_data}"
        }

    with open(video_path, "rb") as vf:
        up_headers = {
            "Content-Range": f"bytes 0-{os.path.getsize(video_path) - 1}/{os.path.getsize(video_path)}",
            "Content-Type": "video/mp4",
        }
        requests.put(upload_url, headers=up_headers, data=vf, timeout=120)

    return {
        "platform": "tiktok",
        "status": "SCHEDULED" if schedule_iso else "PUBLISHED",
        "publish_id": publish_id,
    }


# ---------------------------------------------------------------------------
# Multi-Platform Dispatcher
# ---------------------------------------------------------------------------

def schedule_clip_to_account(
    account_id: str,
    clip_path: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
    target_time_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Assign, queue, and dispatch a clip across all enabled platforms for an account."""
    acc = get_account(account_id)
    if not acc:
        raise ValueError(f"Account '{account_id}' not found.")

    if not os.path.isfile(clip_path):
        raise FileNotFoundError(f"Clip file not found: {clip_path}")

    # Check if clip is already assigned to an account
    prev_acc = get_assigned_clip_account(clip_path)
    if prev_acc and prev_acc != account_id:
        raise ValueError(f"This clip was already scheduled to '{prev_acc}'. Unique clips are enforced.")

    # Determine schedule time
    if not target_time_iso:
        date_str, time_str = calculate_next_slot(account_id)
        target_time_iso = f"{date_str}T{time_str}:00"

    # Enforce daily limit (default 4 clips/day)
    day_str = target_time_iso.split("T")[0]
    curr_count = get_daily_scheduled_count(account_id, day_str)
    max_clips = int(acc.get("max_clips_per_day", 4))
    if curr_count >= max_clips:
        raise ValueError(
            f"Account '{acc.get('name')}' already reached its daily maximum of {max_clips} clips for {day_str}."
        )

    # Load tailored metadata if present
    base_no_ext = os.path.splitext(clip_path)[0]
    meta_json = f"{base_no_ext}_metadata.json"
    plat_meta = {}
    if os.path.isfile(meta_json):
        try:
            with open(meta_json, "r", encoding="utf-8") as mf:
                mdata = json.load(mf)
                plat_meta = mdata.get("platforms") or {}
                if not title:
                    title = mdata.get("hook_title") or os.path.basename(clip_path)
        except Exception:
            pass

    if not title:
        title = os.path.splitext(os.path.basename(clip_path))[0]
    if not description:
        description = title

    platforms_cfg = acc.get("platforms") or {}
    results = {}

    # 1. YouTube Shorts
    if (platforms_cfg.get("youtube") or {}).get("enabled"):
        try:
            yt_title = plat_meta.get("youtube_title") or title
            yt_desc = plat_meta.get("youtube_description") or description
            results["youtube"] = upload_to_youtube(
                account_id=account_id,
                video_path=clip_path,
                title=yt_title,
                description=yt_desc,
                tags=tags,
                schedule_iso=target_time_iso,
            )
        except Exception as e:
            logger.error(f"YouTube upload failed: {e}")
            results["youtube"] = {"platform": "youtube", "status": "FAILED", "error": str(e)}

    # 2. Instagram Reels
    if (platforms_cfg.get("instagram") or {}).get("enabled"):
        try:
            ig_caption = plat_meta.get("reels_caption") or description
            results["instagram"] = upload_to_meta_reels(
                account_id=account_id,
                platform="instagram",
                video_path=clip_path,
                caption=ig_caption,
                schedule_iso=target_time_iso,
            )
        except Exception as e:
            logger.error(f"Instagram upload failed: {e}")
            results["instagram"] = {"platform": "instagram", "status": "FAILED", "error": str(e)}

    # 3. Facebook Reels
    if (platforms_cfg.get("facebook") or {}).get("enabled"):
        try:
            fb_caption = plat_meta.get("reels_caption") or description
            results["facebook"] = upload_to_meta_reels(
                account_id=account_id,
                platform="facebook",
                video_path=clip_path,
                caption=fb_caption,
                schedule_iso=target_time_iso,
            )
        except Exception as e:
            logger.error(f"Facebook upload failed: {e}")
            results["facebook"] = {"platform": "facebook", "status": "FAILED", "error": str(e)}

    # 4. TikTok
    if (platforms_cfg.get("tiktok") or {}).get("enabled"):
        try:
            tt_caption = plat_meta.get("tiktok_caption") or description
            results["tiktok"] = upload_to_tiktok(
                account_id=account_id,
                video_path=clip_path,
                title=tt_caption,
                schedule_iso=target_time_iso,
            )
        except Exception as e:
            logger.error(f"TikTok upload failed: {e}")
            results["tiktok"] = {"platform": "tiktok", "status": "FAILED", "error": str(e)}

    # Add entry to schedule queue
    entry = {
        "id": f"post_{int(time.time())}_{account_id}",
        "account_id": account_id,
        "account_name": acc.get("name"),
        "clip_path": clip_path,
        "clip_name": os.path.basename(clip_path),
        "title": title,
        "scheduled_time": target_time_iso,
        "status": "SCHEDULED",
        "platforms": results,
        "created_at": datetime.datetime.now().isoformat(),
    }

    queue = load_schedule_queue()
    queue.append(entry)
    save_schedule_queue(queue)

    return entry
