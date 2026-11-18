"""clipper/social.py - accounts, a post plan, and (optional) API upload for YouTube / Instagram / TikTok / Facebook.

Two ways to publish, and the first one is the default:

PLAN mode (default, free, nothing to log into here)
    Studio keeps the plan: which clip goes to which account, when, with the caption for every
    platform. At posting time you click "Open YouTube / TikTok / Meta", paste the caption, pick
    the file and use that site's OWN scheduler. Those schedulers run in the platform's cloud,
    so the laptop can be off once you have scheduled. You tick each platform "posted".

AUTO mode (advanced, needs a login per platform and has platform limits - see notes below)
    Studio uploads through the official APIs. What each platform really allows:
      * YouTube   - upload + status.publishAt (cloud-held). A Google API project that has not
                    passed Google's audit gets its uploads locked to private.
      * Facebook  - Page Reels with scheduled_publish_time (cloud-held).
      * Instagram - the API has no scheduling. Studio holds the post and publishes it at the
                    time while Studio is running.
      * TikTok    - the API has no scheduling and un-audited apps can only post private.

Every platform result is stored truthfully (PLANNED / UPLOADED_SCHEDULED / HELD / UPLOADED_PRIVATE /
PUBLISHED / POSTED / FAILED). A clip only counts against the daily limit and is only locked to an
account while at least one platform of that post did not fail.

Secrets (tokens, client secret) live in config/tokens/ and config/youtube_client_secret.json.
Both are git-ignored.
"""
import datetime
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("clipper.social")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
TOKENS_DIR = os.path.join(CONFIG_DIR, "tokens")
CLIENT_SECRET_FILE = os.path.join(CONFIG_DIR, "youtube_client_secret.json")
ACCOUNTS_FILE = os.path.join(CONFIG_DIR, "social_accounts.yaml")
SCHEDULE_QUEUE_FILE = os.path.join(CONFIG_DIR, "scheduled_posts.json")

PLATFORMS = ("youtube", "instagram", "tiktok", "facebook")
DEFAULT_SLOTS = ["11:00", "15:00", "19:00", "22:00"]
GRAPH = "https://graph.facebook.com/v21.0"

# Where each platform's own scheduler lives (opened by the "Open" buttons).
PLATFORM_LINKS = {
    "youtube": "https://www.youtube.com/upload",
    "tiktok": "https://www.tiktok.com/tiktokstudio/upload",
    "instagram": "https://business.facebook.com/latest/home",
    "facebook": "https://business.facebook.com/latest/home",
}

# statuses
PLANNED, POSTED = "PLANNED", "POSTED"
UPLOADING, HELD = "UPLOADING", "HELD"
UPLOADED_SCHEDULED, UPLOADED_PRIVATE, PUBLISHED = "UPLOADED_SCHEDULED", "UPLOADED_PRIVATE", "PUBLISHED"
FAILED, SKIPPED = "FAILED", "SKIPPED"
_OK_STATES = {PLANNED, POSTED, UPLOADING, HELD, UPLOADED_SCHEDULED, UPLOADED_PRIVATE, PUBLISHED}

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _aware(iso: str) -> datetime.datetime:
    """Parse an ISO time. A time without a zone means the laptop's local time."""
    dt = datetime.datetime.fromisoformat(iso)
    return dt.astimezone() if dt.tzinfo is None else dt


def _now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


def _local_day(iso: str) -> str:
    return _aware(iso).date().isoformat()


def _norm(p: str) -> str:
    return os.path.normpath(os.path.abspath(p or "")).lower()


def _empty_platforms() -> Dict[str, Dict[str, Any]]:
    return {p: {"enabled": False, "handle": ""} for p in PLATFORMS}


def _token_path(account_id: str, platform: str) -> str:
    return os.path.join(TOKENS_DIR, f"{platform}_{account_id}.json")


# ---------------------------------------------------------------------------
# accounts (no secrets in here - this file is just names and handles)
# ---------------------------------------------------------------------------

def load_accounts() -> Dict[str, Dict[str, Any]]:
    if not os.path.isfile(ACCOUNTS_FILE):
        return {}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("accounts") or {}
    except Exception as e:
        logger.error("Error loading %s: %s", ACCOUNTS_FILE, e)
        return {}


def save_accounts(accounts: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump({"accounts": accounts}, f, sort_keys=False, allow_unicode=True)


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    return load_accounts().get(account_id)


def upsert_account(account_id: Optional[str], data: Dict[str, Any]) -> str:
    """Create or update an account. Only names, handles, limit and times are stored here."""
    accounts = load_accounts()
    aid = (account_id or "").strip() or f"acc_{int(time.time())}"
    acc = accounts.get(aid, {})
    for k in ("name", "max_clips_per_day", "schedule_slots"):
        if k in data and data[k] not in (None, ""):
            acc[k] = data[k]
    if isinstance(data.get("platforms"), dict):
        plats = acc.get("platforms") or _empty_platforms()
        for p in PLATFORMS:
            if p in data["platforms"]:
                new = data["platforms"][p] or {}
                cur = plats.get(p) or {"enabled": False, "handle": ""}
                cur["enabled"] = bool(new.get("enabled", cur.get("enabled")))
                handle = new.get("handle", new.get("channel_name", new.get("account_name", new.get("page_name", cur.get("handle", "")))))
                cur["handle"] = str(handle or "").strip()
                plats[p] = cur
        acc["platforms"] = plats
    acc["id"] = aid
    acc.setdefault("name", "My account")
    acc.setdefault("max_clips_per_day", 4)
    acc.setdefault("schedule_slots", list(DEFAULT_SLOTS))
    acc.setdefault("platforms", _empty_platforms())
    acc["max_clips_per_day"] = max(1, min(20, int(acc["max_clips_per_day"])))
    accounts[aid] = acc
    save_accounts(accounts)
    return aid


def delete_account(account_id: str) -> bool:
    accounts = load_accounts()
    if account_id not in accounts:
        return False
    del accounts[account_id]
    save_accounts(accounts)
    for p in PLATFORMS:
        try:
            os.remove(_token_path(account_id, p))
        except OSError:
            pass
    return True


# ---------------------------------------------------------------------------
# logins (stored apart from the account list)
# ---------------------------------------------------------------------------

def save_credentials(account_id: str, platform: str, creds: Dict[str, Any]) -> None:
    os.makedirs(TOKENS_DIR, exist_ok=True)
    path = _token_path(account_id, platform)
    clean = {k: str(v).strip() for k, v in creds.items() if v not in (None, "")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_credentials(account_id: str, platform: str) -> Dict[str, Any]:
    path = _token_path(account_id, platform)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def disconnect(account_id: str, platform: str) -> bool:
    try:
        os.remove(_token_path(account_id, platform))
        return True
    except OSError:
        return False


def connection_state(account_id: str) -> Dict[str, bool]:
    """Which platforms have a login stored (only matters for AUTO mode)."""
    yt = os.path.isfile(os.path.join(TOKENS_DIR, f"yt_token_{account_id}.json")) or os.path.isfile(_token_path(account_id, "youtube"))
    ig = load_credentials(account_id, "instagram")
    fb = load_credentials(account_id, "facebook")
    tt = load_credentials(account_id, "tiktok")
    return {
        "youtube": yt,
        "instagram": bool(ig.get("access_token") and ig.get("ig_user_id")),
        "facebook": bool(fb.get("access_token") and fb.get("page_id")),
        "tiktok": bool(tt.get("access_token")),
    }


def has_client_secret() -> bool:
    return os.path.isfile(CLIENT_SECRET_FILE)


def save_client_secret(raw: bytes) -> None:
    """Store the Google OAuth client file after checking it really is one."""
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not (data.get("installed") or data.get("web")):
        raise ValueError("That file is not a Google OAuth client file (it needs an 'installed' section). "
                         "Create an OAuth client of type 'Desktop app' and download its JSON.")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CLIENT_SECRET_FILE, "wb") as f:
        f.write(raw)
    try:
        os.chmod(CLIENT_SECRET_FILE, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# the plan (queue)
# ---------------------------------------------------------------------------

def load_schedule_queue() -> List[Dict[str, Any]]:
    with _lock:
        if not os.path.isfile(SCHEDULE_QUEUE_FILE):
            return []
        try:
            with open(SCHEDULE_QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or []
        except Exception as e:
            logger.error("Error reading %s: %s", SCHEDULE_QUEUE_FILE, e)
            return []


def save_schedule_queue(queue: List[Dict[str, Any]]) -> None:
    with _lock:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = SCHEDULE_QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
        os.replace(tmp, SCHEDULE_QUEUE_FILE)


def entry_status(entry: Dict[str, Any]) -> str:
    """One word for the whole post, worked out from what each platform really did."""
    states = [p.get("status") for p in (entry.get("platforms") or {}).values()]
    if not states:
        return "EMPTY"
    if all(s == FAILED for s in states):
        return "FAILED"
    if any(s == UPLOADING for s in states):
        return "UPLOADING"
    if any(s == FAILED for s in states):
        return "PARTIAL"
    if all(s in (POSTED, PUBLISHED) for s in states):
        return "DONE"
    if any(s in (HELD, UPLOADED_SCHEDULED) for s in states):
        return "SCHEDULED"
    if any(s == UPLOADED_PRIVATE for s in states):
        return "PRIVATE"
    return "TO POST"


def _is_active(entry: Dict[str, Any]) -> bool:
    """Does this post still use up a slot and keep its clip locked?"""
    return entry_status(entry) not in ("FAILED", "EMPTY")


def get_daily_scheduled_count(account_id: str, target_date_str: Optional[str] = None) -> int:
    day = target_date_str or _now().date().isoformat()
    n = 0
    for post in load_schedule_queue():
        if post.get("account_id") == account_id and _is_active(post):
            try:
                if _local_day(post.get("scheduled_time") or "") == day:
                    n += 1
            except ValueError:
                continue
    return n


def get_assigned_clip_account(clip_path: str) -> Optional[str]:
    want = _norm(clip_path)
    for post in load_schedule_queue():
        if _is_active(post) and _norm(post.get("clip_path")) == want:
            return post.get("account_id")
    return None


def delete_schedule_entry(post_id: str) -> bool:
    with _lock:
        queue = load_schedule_queue()
        keep = [p for p in queue if p.get("id") != post_id]
        if len(keep) == len(queue):
            return False
        save_schedule_queue(keep)
        return True


def calculate_next_slot(account_id: str) -> Tuple[str, str]:
    """First free (date, HH:MM) for an account that respects its daily limit, looking up to 30 days ahead."""
    acc = get_account(account_id) or {}
    max_per_day = int(acc.get("max_clips_per_day", 4))
    slots = sorted(acc.get("schedule_slots") or DEFAULT_SLOTS)
    now = _now()
    taken: Dict[str, set] = {}
    for post in load_schedule_queue():
        if post.get("account_id") == account_id and _is_active(post):
            try:
                dt = _aware(post["scheduled_time"])
            except (KeyError, ValueError):
                continue
            taken.setdefault(dt.date().isoformat(), set()).add(dt.strftime("%H:%M"))
    for add in range(0, 31):
        day = (now + datetime.timedelta(days=add)).date()
        used = taken.get(day.isoformat(), set())
        if len(used) >= max_per_day:
            continue
        for s in slots:
            h, m = map(int, s.split(":"))
            slot_dt = now.replace(year=day.year, month=day.month, day=day.day, hour=h, minute=m, second=0, microsecond=0)
            if slot_dt > now + datetime.timedelta(minutes=15) and s not in used:
                return day.isoformat(), s
    day = (now + datetime.timedelta(days=1)).date().isoformat()
    return day, slots[0] if slots else "12:00"


# ---------------------------------------------------------------------------
# captions per platform
# ---------------------------------------------------------------------------

def build_captions(title: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, str]]:
    """Per-platform title/caption, from the clip's generated captions when there are some."""
    m = meta or {}
    title = (title or "").strip() or "Clip"
    yt_title = m.get("youtube_title") or title
    if "#shorts" not in yt_title.lower():
        yt_title = f"{yt_title[:90]} #Shorts"
    reels = m.get("reels_caption") or title
    return {
        "youtube": {"title": yt_title[:100], "text": m.get("youtube_description") or title},
        "instagram": {"title": "", "text": reels},
        "facebook": {"title": "", "text": reels},
        "tiktok": {"title": "", "text": m.get("tiktok_caption") or title},
    }


# ---------------------------------------------------------------------------
# platform connectors (AUTO mode)
# ---------------------------------------------------------------------------

def connect_youtube(account_id: str) -> None:
    """Run Google's sign-in in the browser. Blocks until the user finishes, so call it in a thread."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not has_client_secret():
        raise FileNotFoundError("Google client file missing. Add it first (Publish -> account -> Auto-upload).")
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, ["https://www.googleapis.com/auth/youtube.upload"])
    creds = flow.run_local_server(port=0, prompt="consent")
    os.makedirs(TOKENS_DIR, exist_ok=True)
    with open(os.path.join(TOKENS_DIR, f"yt_token_{account_id}.json"), "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def _youtube_service(account_id: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    token_file = os.path.join(TOKENS_DIR, f"yt_token_{account_id}.json")
    if not os.path.isfile(token_file):
        raise PermissionError("YouTube is not connected for this account.")
    creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        else:
            raise PermissionError("YouTube login expired. Connect it again.")
    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(account_id: str, video_path: str, title: str, description: str,
                      tags: Optional[List[str]] = None, schedule_iso: Optional[str] = None) -> Dict[str, Any]:
    from googleapiclient.http import MediaFileUpload
    yt = _youtube_service(account_id)
    status: Dict[str, Any] = {"selfDeclaredMadeForKids": False}
    if schedule_iso:
        status["privacyStatus"] = "private"
        status["publishAt"] = _aware(schedule_iso).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        status["privacyStatus"] = "public"
    body = {"snippet": {"title": title[:100], "description": description[:5000],
                        "tags": tags or ["Shorts", "Gaming"], "categoryId": "20"}, "status": status}
    request = yt.videos().insert(part="snippet,status", body=body,
                                 media_body=MediaFileUpload(video_path, chunksize=-1, resumable=True))
    response = None
    while response is None:
        _, response = request.next_chunk()
    vid = response.get("id")
    return {"status": UPLOADED_SCHEDULED if schedule_iso else PUBLISHED, "url": f"https://youtu.be/{vid}", "external_id": vid,
            "note": ("Goes public at the set time (YouTube holds it). If it stays private, your Google project is not "
                     "audited yet - use Plan mode for YouTube." if schedule_iso else "Published.")}


def _graph_error(data: Dict[str, Any]) -> Optional[str]:
    err = data.get("error")
    if err:
        return str(err.get("message") or err)[:300]
    return None


def upload_to_facebook_reel(account_id: str, video_path: str, caption: str, schedule_iso: Optional[str] = None) -> Dict[str, Any]:
    import requests
    c = load_credentials(account_id, "facebook")
    token, page = c.get("access_token"), c.get("page_id")
    if not (token and page):
        raise PermissionError("Facebook Page ID / token not saved for this account.")
    start = requests.post(f"{GRAPH}/{page}/video_reels", params={"upload_phase": "start", "access_token": token}, timeout=30).json()
    if _graph_error(start) or not start.get("upload_url"):
        raise RuntimeError(_graph_error(start) or f"start failed: {start}")
    size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        up = requests.post(start["upload_url"], headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
                           data=f, timeout=600).json()
    if not up.get("success", True) and _graph_error(up):
        raise RuntimeError(_graph_error(up))
    finish = {"upload_phase": "finish", "video_id": start["video_id"], "description": caption[:2000], "access_token": token}
    if schedule_iso:
        when = _aware(schedule_iso)
        if when < _now() + datetime.timedelta(minutes=10):
            raise ValueError("Facebook needs the time to be at least 10 minutes away.")
        finish.update(video_state="SCHEDULED", scheduled_publish_time=int(when.timestamp()))
    else:
        finish["video_state"] = "PUBLISHED"
    fin = requests.post(f"{GRAPH}/{page}/video_reels", params=finish, timeout=60).json()
    if _graph_error(fin) or fin.get("success") is False:
        raise RuntimeError(_graph_error(fin) or f"finish failed: {fin}")
    return {"status": UPLOADED_SCHEDULED if schedule_iso else PUBLISHED, "external_id": start["video_id"],
            "note": "Facebook holds it and publishes at the set time." if schedule_iso else "Published."}


def upload_to_instagram_reel(account_id: str, video_path: str, caption: str) -> Dict[str, Any]:
    """Publish a Reel now. Instagram's API cannot schedule, so Studio calls this when the time arrives."""
    import requests
    c = load_credentials(account_id, "instagram")
    token, ig = c.get("access_token"), c.get("ig_user_id")
    if not (token and ig):
        raise PermissionError("Instagram user ID / token not saved for this account.")
    cont = requests.post(f"{GRAPH}/{ig}/media", data={"media_type": "REELS", "upload_type": "resumable",
                                                      "caption": caption[:2200], "access_token": token}, timeout=30).json()
    if _graph_error(cont) or not cont.get("uri"):
        raise RuntimeError(_graph_error(cont) or f"container failed: {cont}")
    size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        up = requests.post(cont["uri"], headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
                           data=f, timeout=600).json()
    if _graph_error(up):
        raise RuntimeError(_graph_error(up))
    for _ in range(60):  # wait until Instagram finishes processing
        st = requests.get(f"{GRAPH}/{cont['id']}", params={"fields": "status_code", "access_token": token}, timeout=30).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video ({code}).")
        time.sleep(5)
    else:
        raise RuntimeError("Instagram took too long to process the video.")
    pub = requests.post(f"{GRAPH}/{ig}/media_publish", data={"creation_id": cont["id"], "access_token": token}, timeout=60).json()
    if _graph_error(pub) or not pub.get("id"):
        raise RuntimeError(_graph_error(pub) or f"publish failed: {pub}")
    return {"status": PUBLISHED, "external_id": pub["id"], "note": "Published."}


def upload_to_tiktok(account_id: str, video_path: str, title: str) -> Dict[str, Any]:
    """TikTok's API has no schedule field, and apps that TikTok has not audited can only post PRIVATE."""
    import requests
    c = load_credentials(account_id, "tiktok")
    token = c.get("access_token")
    if not token:
        raise PermissionError("TikTok token not saved for this account.")
    size = os.path.getsize(video_path)
    body = {"post_info": {"title": title[:150], "privacy_level": "SELF_ONLY"},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}}
    init = requests.post("https://open.tiktokapis.com/v2/post/publish/video/init/",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
                         json=body, timeout=30).json()
    err = init.get("error") or {}
    data = init.get("data") or {}
    if err.get("code") not in (None, "ok") or not data.get("upload_url"):
        raise RuntimeError(str(err.get("message") or init)[:300])
    with open(video_path, "rb") as f:
        r = requests.put(data["upload_url"], headers={"Content-Range": f"bytes 0-{size - 1}/{size}", "Content-Type": "video/mp4"},
                         data=f, timeout=600)
    if r.status_code >= 300:
        raise RuntimeError(f"TikTok upload failed ({r.status_code}).")
    return {"status": UPLOADED_PRIVATE, "external_id": data.get("publish_id"),
            "note": "Uploaded as PRIVATE. Open TikTok and make it public / schedule it in TikTok Studio."}


# ---------------------------------------------------------------------------
# adding a post to the plan
# ---------------------------------------------------------------------------

def schedule_clip_to_account(account_id: str, clip_path: str, title: Optional[str] = None,
                             description: Optional[str] = None, tags: Optional[List[str]] = None,
                             target_time_iso: Optional[str] = None, mode: str = "plan",
                             meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Add one clip to an account's plan. In "auto" mode the API work then runs in the background."""
    acc = get_account(account_id)
    if not acc:
        raise ValueError(f"Account '{account_id}' not found.")
    if not os.path.isfile(clip_path):
        raise FileNotFoundError(f"Clip file not found: {clip_path}")
    if mode not in ("plan", "auto"):
        raise ValueError("mode must be 'plan' or 'auto'")

    enabled = [p for p in PLATFORMS if ((acc.get("platforms") or {}).get(p) or {}).get("enabled")]
    if not enabled:
        raise ValueError("Switch on at least one platform for this account first.")

    with _lock:
        prev = get_assigned_clip_account(clip_path)
        if prev and prev != account_id:
            name = (get_account(prev) or {}).get("name", prev)
            raise ValueError(f"This clip is already planned for '{name}'. Each clip goes to one account.")
        if prev == account_id:
            raise ValueError("This clip is already in this account's plan.")

        if not target_time_iso:
            d, t = calculate_next_slot(account_id)
            target_time_iso = f"{d}T{t}:00"
        when = _aware(target_time_iso)
        day = when.date().isoformat()
        cap = int(acc.get("max_clips_per_day", 4))
        if get_daily_scheduled_count(account_id, day) >= cap:
            raise ValueError(f"'{acc.get('name')}' already has {cap} clips on {day}. Pick another day.")

        title = (title or "").strip() or os.path.splitext(os.path.basename(clip_path))[0]
        caps = build_captions(title, meta)
        if description:
            for p in caps.values():
                p["text"] = description
        conn = connection_state(account_id)
        plats: Dict[str, Dict[str, Any]] = {}
        for p in enabled:
            slot = dict(caps[p])
            slot["link"] = PLATFORM_LINKS[p]
            if mode == "auto" and conn.get(p):
                slot["status"] = UPLOADING if p in ("youtube", "facebook", "tiktok") else HELD
                slot["note"] = ("Uploading..." if slot["status"] == UPLOADING else
                                "Studio publishes this at the set time (Instagram cannot schedule). Keep Studio running.")
            else:
                slot["status"] = PLANNED
                slot["note"] = ("Not connected - post it by hand." if mode == "auto" else "Open the site, paste the caption, schedule it there.")
            plats[p] = slot
        entry = {
            "id": f"post_{uuid.uuid4().hex[:10]}",
            "account_id": account_id,
            "account_name": acc.get("name"),
            "clip_path": clip_path,
            "clip_name": os.path.basename(clip_path),
            "title": title,
            "tags": tags or [],
            "scheduled_time": when.isoformat(timespec="seconds"),
            "mode": mode,
            "platforms": plats,
            "created_at": _now().isoformat(timespec="seconds"),
        }
        queue = load_schedule_queue()
        queue.append(entry)
        save_schedule_queue(queue)

    if any(p["status"] == UPLOADING for p in plats.values()):
        threading.Thread(target=_run_uploads, args=(entry["id"],), daemon=True, name=f"upload-{entry['id']}").start()
    entry["status"] = entry_status(entry)
    return entry


def _set_platform(post_id: str, platform: str, **fields: Any) -> None:
    with _lock:
        queue = load_schedule_queue()
        for e in queue:
            if e.get("id") == post_id and platform in (e.get("platforms") or {}):
                e["platforms"][platform].update(fields)
        save_schedule_queue(queue)


def _find(post_id: str) -> Optional[Dict[str, Any]]:
    return next((e for e in load_schedule_queue() if e.get("id") == post_id), None)


def _run_uploads(post_id: str) -> None:
    """Background: do the API uploads for one post and record the TRUE result of each platform."""
    e = _find(post_id)
    if not e:
        return
    path, when, aid = e["clip_path"], e["scheduled_time"], e["account_id"]
    for p, slot in list(e["platforms"].items()):
        if slot.get("status") != UPLOADING:
            continue
        try:
            if p == "youtube":
                r = upload_to_youtube(aid, path, slot["title"] or e["title"], slot["text"], e.get("tags"), when)
            elif p == "facebook":
                r = upload_to_facebook_reel(aid, path, slot["text"], when)
            else:
                r = upload_to_tiktok(aid, path, slot["text"])
            _set_platform(post_id, p, **r)
        except Exception as ex:  # noqa: BLE001 - shown to the user as-is
            logger.warning("%s upload failed for %s: %s", p, post_id, ex)
            _set_platform(post_id, p, status=FAILED, note=str(ex)[:300])


def publish_due_posts() -> int:
    """Called every minute while Studio runs: publish Instagram posts whose time has come."""
    done = 0
    now = _now()
    for e in load_schedule_queue():
        slot = (e.get("platforms") or {}).get("instagram")
        if not slot or slot.get("status") != HELD:
            continue
        try:
            if _aware(e["scheduled_time"]) > now:
                continue
        except ValueError:
            continue
        _set_platform(e["id"], "instagram", status=UPLOADING, note="Publishing...")
        try:
            r = upload_to_instagram_reel(e["account_id"], e["clip_path"], slot["text"])
            late = (now - _aware(e["scheduled_time"])).total_seconds() > 600
            if late:
                r["note"] = "Published late - Studio was closed at the set time."
            _set_platform(e["id"], "instagram", **r)
        except Exception as ex:  # noqa: BLE001
            _set_platform(e["id"], "instagram", status=FAILED, note=str(ex)[:300])
        done += 1
    return done


def start_due_loop(interval: int = 60) -> threading.Thread:
    def loop():
        while True:
            try:
                publish_due_posts()
            except Exception as ex:  # noqa: BLE001
                logger.warning("due-loop error: %s", ex)
            time.sleep(interval)
    t = threading.Thread(target=loop, daemon=True, name="publish-due-loop")
    t.start()
    return t


def mark_platform(post_id: str, platform: str, posted: bool = True) -> bool:
    """User ticks a platform as posted (Plan mode), or un-ticks it."""
    e = _find(post_id)
    if not e or platform not in (e.get("platforms") or {}):
        return False
    _set_platform(post_id, platform, status=POSTED if posted else PLANNED,
                  note="Posted." if posted else "Open the site, paste the caption, schedule it there.")
    return True


def retry_platform(post_id: str, platform: str) -> bool:
    """Try a failed platform again (AUTO) or turn it back into a manual to-do."""
    e = _find(post_id)
    if not e or platform not in (e.get("platforms") or {}) or e["platforms"][platform].get("status") != FAILED:
        return False
    if e.get("mode") == "auto" and connection_state(e["account_id"]).get(platform):
        if platform == "instagram":
            _set_platform(post_id, platform, status=HELD, note="Will publish at the set time.")
        else:
            _set_platform(post_id, platform, status=UPLOADING, note="Uploading...")
            threading.Thread(target=_run_uploads, args=(post_id,), daemon=True).start()
    else:
        _set_platform(post_id, platform, status=PLANNED, note="Post it by hand.")
    return True


def queue_view() -> List[Dict[str, Any]]:
    """Queue with the one-word status filled in, newest day first."""
    out = []
    for e in load_schedule_queue():
        e = dict(e)
        e["status"] = entry_status(e)
        out.append(e)
    out.sort(key=lambda x: x.get("scheduled_time") or "")
    return out
