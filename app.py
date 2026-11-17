"""app.py - Local Stream Clipper Web Application (Streamlit).

Clean, fast, local stream-to-shorts tool for HP Omen 16 (RTX 5070 Ti).
Adheres strictly to the Golden Rule: no fake data, no placeholder values.
"""
import json
import os
import re
import shutil
import sys
import time

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import streamlit as st

from clipper.campaigns import (
    DEFAULT_RISK_WORDS,
    delete_campaign,
    list_campaigns,
    load_campaign,
    save_campaign,
)
from clipper.cutter import render_vertical_clip
from clipper.downloader import get_available_qualities
from clipper.process_manager import get_job_logs
from clipper.qc import generate_platform_metadata, perform_qc_check
from clipper.queue import (
    add_job,
    cancel_all,
    cancel_job,
    check_duplicate_job,
    clear_finished_jobs,
    confirm_render_moments,
    delete_job,
    ensure_worker_running,
    load_jobs,
    pause_job,
    resume_job,
    save_jobs,
)
from clipper.utils import setup_cuda_dlls

# Initialize hardware environment and background worker
setup_cuda_dlls()
ensure_worker_running()

PREFS_FILE = "user_prefs.json"


def load_user_prefs() -> dict:
    if os.path.isfile(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_quality": "1080p"}


def save_user_prefs(prefs: dict):
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass


st.set_page_config(
    page_title="Stream Clipper",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Clean, uncluttered dark theme CSS hiding deploy button, toolbar, and footer
st.markdown("""
<style>
    /* Hide Deploy button, Streamlit toolbar, status widget, and footer */
    #MainMenu {visibility: hidden; display: none !important;}
    header {visibility: hidden; display: none !important;}
    footer {visibility: hidden; display: none !important;}
    [data-testid="stToolbar"] {visibility: hidden; display: none !important;}
    [data-testid="stDecoration"] {visibility: hidden; display: none !important;}
    [data-testid="stStatusWidget"] {visibility: hidden; display: none !important;}

    /* Consistent dark typography & generous spacing */
    .stApp {
        background-color: #0d1117;
        color: #c9d1d9;
    }
    
    /* Clean Cards */
    .job-card {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 18px;
        margin-bottom: 16px;
    }

    .clip-card {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 16px;
    }

    .stButton>button {
        border-radius: 6px;
        font-weight: 500;
        font-size: 14px;
        padding: 6px 14px;
    }

    /* Status Badges */
    .status-badge {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 12px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .badge-running { background-color: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb; }
    .badge-completed { background-color: #23863622; color: #3fb950; border: 1px solid #238636; }
    .badge-paused { background-color: #d2992222; color: #d29922; border: 1px solid #d29922; }
    .badge-cancelled { background-color: #da363322; color: #f85149; border: 1px solid #da3633; }
    .badge-failed { background-color: #da363322; color: #f85149; border: 1px solid #da3633; }
    .badge-queued { background-color: #6e768122; color: #8b949e; border: 1px solid #6e7681; }
    .badge-flagged { background-color: #d2992222; color: #e3b341; border: 1px solid #d29922; font-size: 11px; padding: 2px 6px; border-radius: 4px; }
    .badge-clean { background-color: #23863622; color: #3fb950; border: 1px solid #238636; font-size: 11px; padding: 2px 6px; border-radius: 4px; }

    /* Clean progress and checklist */
    .stage-item { font-size: 13px; font-weight: 500; }
    .stage-done { color: #3fb950; font-weight: 600; }
    .stage-running { color: #58a6ff; font-weight: 600; }
    .stage-waiting { color: #6e7681; font-weight: 400; }
</style>
""", unsafe_allow_html=True)


# ==========================================
# SIDEBAR: HARDWARE STATUS & DEVELOPER TOOLS
# ==========================================
with st.sidebar:
    st.markdown("### Stream Clipper")

    # Real hardware status line
    try:
        import torch
        has_gpu = torch.cuda.is_available()
    except Exception:
        has_gpu = False

    _, _, free_b = shutil.disk_usage(".")
    free_gb = int(free_b / (1024 ** 3))

    status_line = f"GPU ready - {free_gb} GB free" if has_gpu else f"CPU mode - {free_gb} GB free"
    st.caption(status_line)

    st.divider()

    # Developer tools collapsed at the very bottom
    with st.expander("Developer tools", expanded=False):
        if st.button("Run Synthetic Dry Run", use_container_width=True):
            jid = add_job(
                vod_input="tests/test_data/synthetic_vod.mp4",
                chat_input="tests/test_data/synthetic_chat.json",
                quality="1080p",
                campaign_id=None,
                mode="MANUAL",
                top=2,
                preset="classic_yellow",
                auto_render=True,
                is_dev_test=True,
            )
            st.success(f"Queued dry run #{jid}")
            time.sleep(0.5)
            st.rerun()


# ==========================================
# MAIN TABS
# ==========================================
tab_new, tab_campaigns, tab_queue, tab_gallery = st.tabs([
    "New Job", "Campaigns", "Queue & Progress", "Clips Gallery"
])


# ==========================================
# TAB 1: NEW JOB (SIMPLE, STRICT ORDER)
# ==========================================
with tab_new:
    # 1. VOD link or file path (supports multiple links, one per line)
    vod_input = st.text_area(
        "VOD link or file path (paste multiple links, one per line)",
        placeholder="https://www.twitch.tv/videos/...\nhttps://youtube.com/watch?v=...",
        height=85,
        key="clean_vod_input",
    )

    raw_lines = [l.strip() for l in vod_input.strip().splitlines() if l.strip()]
    if len(raw_lines) > 1:
        st.info(f"{len(raw_lines)} links detected. Each link will be queued as an individual job.")

    # 2. Campaign dropdown
    campaigns = list_campaigns("campaigns")
    camp_map = {"none": "No campaign"}
    for c in campaigns:
        label = c["name"]
        if c.get("streamer"):
            label += f" ({c['streamer']})"
        camp_map[c["id"]] = label

    selected_camp_id = st.selectbox(
        "Campaign",
        options=list(camp_map.keys()),
        format_func=lambda x: camp_map[x],
        key="clean_camp_select",
    )

    # 3. Quality (appears ONLY after link is checked)
    first_url = raw_lines[0] if raw_lines else ""
    clean_url = first_url
    selected_quality = "1080p"
    quality_ready = True

    if clean_url:
        if "quality_cache" not in st.session_state:
            st.session_state["quality_cache"] = {}

        if clean_url not in st.session_state["quality_cache"]:
            with st.spinner("Checking available video qualities..."):
                st.session_state["quality_cache"][clean_url] = get_available_qualities(clean_url)

        qualities = st.session_state["quality_cache"].get(clean_url, [])
        if qualities:
            user_prefs = load_user_prefs()
            saved_q = user_prefs.get("last_quality", "1080p")
            q_ids = [q["id"] for q in qualities]
            default_idx = (
                q_ids.index(saved_q)
                if saved_q in q_ids
                else (q_ids.index("1080p") if "1080p" in q_ids else 0)
            )

            selected_q_obj = st.selectbox(
                "Quality",
                options=qualities,
                index=default_idx,
                format_func=lambda q: q["label"],
                key="clean_quality_select",
            )
            selected_quality = selected_q_obj["id"]
            st.caption("Vertical crop zooms in, so lower quality looks blurry.")

            # Disk space check against real free space
            size_mb = selected_q_obj.get("size_mb", 0.0)
            if size_mb > 0:
                size_gb = size_mb / 1024.0
                if size_gb > free_gb:
                    st.warning(
                        f"Warning: Download size (~{size_gb:.1f} GB) exceeds free disk space ({free_gb} GB)."
                    )

    # Collapsed Advanced Settings
    with st.expander("Advanced settings", expanded=False):
        chat_file = st.text_input(
            "Chat file path (optional)",
            placeholder="Path to downloaded chat JSON",
            key="adv_chat_file",
        )
        caption_preset = st.selectbox(
            "Caption preset",
            ["classic_yellow", "neon_green", "cyber_cyan", "clean_white"],
            index=0,
            key="adv_caption_preset",
        )

        col_mode, col_count = st.columns(2)
        with col_mode:
            clip_mode = st.radio(
                "Clip count mode",
                ["Auto", "Manual"],
                index=0,
                horizontal=True,
                key="adv_clip_mode",
            )
        with col_count:
            if clip_mode == "Manual":
                manual_clips = st.number_input(
                    "Number of clips",
                    min_value=1,
                    max_value=500,
                    value=5,
                    key="adv_manual_clips",
                )
            else:
                manual_clips = None

        col_safe, col_sens = st.columns(2)
        with col_safe:
            safety_max = st.number_input(
                "Safety maximum",
                min_value=1,
                max_value=500,
                value=50,
                key="adv_safety_max",
            )
        with col_sens:
            sensitivity = st.selectbox(
                "Sensitivity",
                ["Medium", "Low", "High"],
                index=0,
                key="adv_sensitivity",
            )
            sens_map = {"Low": 2.5, "Medium": 2.0, "High": 1.5}
            z_thresh = sens_map[sensitivity]

        col_rev, col_cpu = st.columns(2)
        with col_rev:
            review_before_render = st.checkbox(
                "Review moments before rendering",
                value=True,
                key="adv_review",
            )
        with col_cpu:
            force_cpu = st.checkbox("Force CPU", value=False, key="adv_force_cpu")

    # Duplicate job confirmation handling
    if "pending_dup" not in st.session_state:
        st.session_state["pending_dup"] = None

    if st.session_state["pending_dup"]:
        st.warning(
            f"A job (#{st.session_state['pending_dup']}) is already queued or running for this source. Start anyway?"
        )
        d_c1, d_c2 = st.columns([1, 4])
        with d_c1:
            if st.button("Start anyway", type="primary", use_container_width=True):
                save_user_prefs({"last_quality": selected_quality})
                camp_arg = None if selected_camp_id == "none" else selected_camp_id
                jid = add_job(
                    vod_input=clean_url,
                    campaign_id=camp_arg,
                    chat_input=chat_file.strip() if chat_file.strip() else None,
                    quality=selected_quality,
                    mode="AUTO" if clip_mode == "Auto" else "MANUAL",
                    top=manual_clips,
                    safety_max=safety_max,
                    z_thresh=z_thresh,
                    preset=caption_preset,
                    use_cpu=force_cpu,
                    auto_render=not review_before_render,
                )
                st.session_state["pending_dup"] = None
                st.success(f"Job #{jid} started.")
                time.sleep(0.5)
                st.rerun()
        with d_c2:
            if st.button("Cancel", use_container_width=True):
                st.session_state["pending_dup"] = None
                st.rerun()

    else:
        # 4. Big "Start" button
        if st.button("Start", type="primary", use_container_width=True):
            if not raw_lines:
                st.error("Please enter a VOD link or file path.")
            elif len(raw_lines) > 1:
                camp_arg = None if selected_camp_id == "none" else selected_camp_id
                save_user_prefs({"last_quality": selected_quality})
                queued_ids = []
                for url_item in raw_lines:
                    jid = add_job(
                        vod_input=url_item,
                        campaign_id=camp_arg,
                        chat_input=chat_file.strip() if chat_file.strip() else None,
                        quality=selected_quality,
                        mode="AUTO" if clip_mode == "Auto" else "MANUAL",
                        top=manual_clips,
                        safety_max=safety_max,
                        z_thresh=z_thresh,
                        preset=caption_preset,
                        use_cpu=force_cpu,
                        auto_render=not review_before_render,
                    )
                    queued_ids.append(jid)
                st.success(f"Queued {len(queued_ids)} jobs: {', '.join(f'#{j}' for j in queued_ids)}")
                time.sleep(0.5)
                st.rerun()
            else:
                camp_arg = None if selected_camp_id == "none" else selected_camp_id
                existing_jid = check_duplicate_job(clean_url, camp_arg)
                if existing_jid:
                    st.session_state["pending_dup"] = existing_jid
                    st.rerun()
                else:
                    save_user_prefs({"last_quality": selected_quality})
                    jid = add_job(
                        vod_input=clean_url,
                        campaign_id=camp_arg,
                        chat_input=chat_file.strip() if chat_file.strip() else None,
                        quality=selected_quality,
                        mode="AUTO" if clip_mode == "Auto" else "MANUAL",
                        top=manual_clips,
                        safety_max=safety_max,
                        z_thresh=z_thresh,
                        preset=caption_preset,
                        use_cpu=force_cpu,
                        auto_render=not review_before_render,
                    )
                    st.success(f"Job #{jid} started.")
                    time.sleep(0.5)
                    st.rerun()


# ==========================================
# TAB 2: CAMPAIGNS (ADD, EDIT, DELETE)
# ==========================================
with tab_campaigns:
    st.markdown("### Campaigns")

    if "editing_campaign_id" not in st.session_state:
        st.session_state["editing_campaign_id"] = None

    edit_cid = st.session_state["editing_campaign_id"]
    edit_data = {}
    if edit_cid:
        try:
            edit_data = load_campaign(edit_cid)
            st.caption(f"Editing campaign: **{edit_data.get('name', edit_cid)}**")
        except Exception:
            st.session_state["editing_campaign_id"] = None
            edit_cid = None

    # Add / Edit Campaign Form
    with st.form("campaign_form", clear_on_submit=False):
        c_name = st.text_input("Name", value=edit_data.get("name", ""))
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            c_streamer = st.text_input("Streamer", value=edit_data.get("streamer", ""))
        with col_c2:
            platforms = ["Twitch", "YouTube", "Kick", "Other"]
            cur_plat = edit_data.get("platform", "Twitch")
            plat_idx = platforms.index(cur_plat) if cur_plat in platforms else 0
            c_platform = st.selectbox("Platform", platforms, index=plat_idx)

        col_l1, col_l2 = st.columns(2)
        with col_l1:
            c_min_len = st.number_input(
                "Minimum clip length (seconds)",
                min_value=5,
                max_value=180,
                value=edit_data.get("min_clip_length", 15),
            )
        with col_l2:
            c_max_len = st.number_input(
                "Maximum clip length (seconds)",
                min_value=10,
                max_value=300,
                value=edit_data.get("max_clip_length", 60),
            )

        existing_tags = " ".join(edit_data.get("required_hashtags", []))
        c_hashtags = st.text_input(
            "Required hashtags (comma or space separated)",
            value=existing_tags,
        )

        c_disclosure = st.text_input(
            "Disclosure text",
            value=edit_data.get("required_disclosure", ""),
            placeholder="e.g. #ad, Sponsored",
        )

        existing_risk = ", ".join(edit_data.get("risk_words", DEFAULT_RISK_WORDS))
        c_risk_words = st.text_input(
            "Risk words (comma separated)",
            value=existing_risk,
        )

        c_rules = st.text_area(
            "Rules and notes",
            value=edit_data.get("rules_note", ""),
            height=100,
        )

        c_overlay = st.text_input(
            "Overlay image path (PNG, optional)",
            value=edit_data.get("overlay", "") or "",
        )

        submit_btn = st.form_submit_button(
            "Save Campaign" if not edit_cid else "Update Campaign"
        )

        if submit_btn:
            if not c_name.strip():
                st.error("Campaign name is required.")
            else:
                # Parse hashtags
                clean_tags = [
                    t.strip()
                    for t in re.split(r"[,\s]+", c_hashtags)
                    if t.strip()
                ]
                clean_risk = [
                    w.strip()
                    for w in c_risk_words.split(",")
                    if w.strip()
                ]

                camp_payload = {
                    "name": c_name.strip(),
                    "streamer": c_streamer.strip(),
                    "platform": c_platform.strip(),
                    "min_clip_length": int(c_min_len),
                    "max_clip_length": int(c_max_len),
                    "required_hashtags": clean_tags,
                    "required_disclosure": c_disclosure.strip(),
                    "risk_words": clean_risk,
                    "rules_note": c_rules.strip(),
                    "overlay": c_overlay.strip() if c_overlay.strip() else None,
                }
                saved_id = save_campaign(camp_payload, campaign_id=edit_cid)
                st.session_state["editing_campaign_id"] = None
                st.success(f"Campaign saved: {saved_id}")
                time.sleep(0.5)
                st.rerun()

    if edit_cid:
        if st.button("Cancel editing"):
            st.session_state["editing_campaign_id"] = None
            st.rerun()

    st.divider()

    # List Existing Campaigns
    current_campaigns = list_campaigns("campaigns")
    if not current_campaigns:
        st.info("No campaigns saved yet. Add a campaign using the form above.")
    else:
        st.markdown("#### Existing Campaigns")
        for camp in current_campaigns:
            cid = camp["id"]
            with st.container():
                c_row1, c_row2 = st.columns([4, 1])
                with c_row1:
                    st.markdown(f"**{camp['name']}** ({camp['streamer']}) — `{camp['platform']}`")
                    st.caption(
                        f"Duration: {camp['min_clip_length']}s-{camp['max_clip_length']}s | "
                        f"Disclosure: `{camp.get('required_disclosure', 'none')}` | "
                        f"Hashtags: {' '.join(camp.get('required_hashtags', [])) or 'none'}"
                    )
                    if camp.get("rules_note"):
                        st.caption(f"Rules: {camp['rules_note']}")
                with c_row2:
                    b_col1, b_col2 = st.columns(2)
                    with b_col1:
                        if st.button("Edit", key=f"edit_c_{cid}"):
                            st.session_state["editing_campaign_id"] = cid
                            st.rerun()
                    with b_col2:
                        if st.button("Delete", key=f"del_c_{cid}"):
                            delete_campaign(cid)
                            st.rerun()
                st.divider()


# ==========================================
# TAB 3: QUEUE & PROGRESS (LIVE FRAGMENT)
# ==========================================
with tab_queue:
    # Controls row
    col_act1, col_act2, col_act3 = st.columns([1, 1, 2])
    with col_act1:
        if st.button("Cancel all", use_container_width=True):
            cancel_all()
            st.rerun()
    with col_act2:
        if st.button("Clear finished", use_container_width=True):
            clear_finished_jobs()
            st.rerun()
    with col_act3:
        if st.button("Refresh", use_container_width=True):
            st.rerun()

    jobs_snapshot = load_jobs()
    has_active_work = any(
        j.get("status") in ("RUNNING", "QUEUED") for j in jobs_snapshot.values()
    )
    fragment_interval = "2s" if has_active_work else None

    @st.fragment(run_every=fragment_interval)
    def render_queue_fragment():
        jobs = load_jobs()
        if not jobs:
            st.info("No jobs in queue.")
            return

        # Overall queue progress
        active_jobs = [
            j for j in jobs.values()
            if j.get("status") in ("RUNNING", "QUEUED", "AWAITING_SELECTION")
        ]
        if active_jobs:
            avg_progress = int(
                sum(j.get("progress", 0) for j in active_jobs) / len(active_jobs)
            )
            st.progress(avg_progress / 100.0)
            st.caption(f"Overall progress: {avg_progress}% ({len(active_jobs)} active)")

        st.divider()

        # Display jobs in reverse chronological order
        for jid in sorted(
            jobs.keys(),
            key=lambda k: jobs[k].get("created_at", ""),
            reverse=True,
        ):
            job = jobs[jid]
            status = job.get("status", "QUEUED")
            progress = job.get("progress", 0)
            stages = job.get("stages", {})
            stage_details = job.get("stage_details", job.get("status_message", ""))
            now = time.time()

            # Status badge class
            badge_class = {
                "RUNNING": "badge-running",
                "COMPLETED": "badge-completed",
                "PAUSED": "badge-paused",
                "CANCELLED": "badge-cancelled",
                "FAILED": "badge-failed",
                "QUEUED": "badge-queued",
                "AWAITING_SELECTION": "badge-paused",
                "INTERRUPTED": "badge-cancelled",
            }.get(status, "badge-queued")

            with st.container():
                hdr_col1, hdr_col2 = st.columns([3, 1.2])
                with hdr_col1:
                    st.markdown(
                        f"**Job #{jid}** — <span class='status-badge {badge_class}'>{status}</span>",
                        unsafe_allow_html=True,
                    )
                    st.caption(
                        f"Source: `{job.get('vod_input')}` | Quality: {job.get('quality', '1080p')} | Mode: {job.get('mode', 'AUTO')}"
                    )
                with hdr_col2:
                    b1, b2 = st.columns(2)
                    if status in ("RUNNING", "QUEUED"):
                        with b1:
                            if st.button("Pause", key=f"q_pause_{jid}", use_container_width=True):
                                pause_job(jid)
                                st.rerun()
                        with b2:
                            if st.button("Cancel", key=f"q_cancel_{jid}", use_container_width=True):
                                cancel_job(jid)
                                st.rerun()

                    elif status == "PAUSED":
                        with b1:
                            if st.button("Resume", key=f"q_res_{jid}", use_container_width=True):
                                resume_job(jid)
                                st.rerun()
                        with b2:
                            if st.button("Cancel", key=f"q_can_p_{jid}", use_container_width=True):
                                cancel_job(jid)
                                st.rerun()

                    elif status == "AWAITING_SELECTION":
                        with b1:
                            if st.button("Pause", key=f"q_pause_sel_{jid}", use_container_width=True):
                                pause_job(jid)
                                st.rerun()
                        with b2:
                            if st.button("Cancel", key=f"q_can_sel_{jid}", use_container_width=True):
                                cancel_job(jid)
                                st.rerun()

                    elif status == "INTERRUPTED":
                        with b1:
                            if st.button("Resume", key=f"q_res_int_{jid}", use_container_width=True):
                                resume_job(jid)
                                st.rerun()
                        with b2:
                            if st.button("Delete", key=f"q_del_int_{jid}", use_container_width=True):
                                delete_job(jid)
                                st.rerun()

                    elif status in ("COMPLETED", "CANCELLED", "FAILED"):
                        with b2:
                            if st.button("Delete", key=f"q_del_{jid}", use_container_width=True):
                                delete_job(jid)
                                st.rerun()

                # Progress & Stage details
                if status in ("RUNNING", "PAUSED", "AWAITING_SELECTION", "QUEUED"):
                    st.progress(progress / 100.0)

                    time_info = []
                    start_time = job.get("start_time")
                    if start_time:
                        elapsed = int(now - start_time)
                        time_info.append(f"Elapsed: {elapsed // 60}m {elapsed % 60:02d}s")
                    eta_sec = job.get("eta_seconds")
                    if eta_sec is not None and eta_sec > 0:
                        time_info.append(f"Remaining: {eta_sec // 60}m {eta_sec % 60:02d}s")

                    heartbeat = job.get("heartbeat", now)
                    diff_hb = int(now - heartbeat)
                    if status == "PAUSED":
                        hb_str = "Paused"
                    elif diff_hb > 60:
                        hb_str = f"Last activity {diff_hb}s ago"
                    else:
                        hb_str = f"Active ({diff_hb}s ago)"

                    info_text = f"{stage_details} ({progress}%)"
                    if time_info:
                        info_text += f" | {' | '.join(time_info)}"
                    info_text += f" | {hb_str}"
                    st.caption(info_text)

                    # Stage checklist
                    def stage_text(st_val: str, label: str) -> str:
                        if st_val == "DONE":
                            return f"<span class='stage-done'>Done: {label}</span>"
                        elif st_val == "RUNNING":
                            return f"<span class='stage-running'>Running: {label}</span>"
                        return f"<span class='stage-waiting'>Waiting: {label}</span>"

                    c1, c2, c3, c4, c5 = st.columns(5)
                    with c1:
                        st.markdown(stage_text(stages.get("download", "WAITING"), "Download"), unsafe_allow_html=True)
                    with c2:
                        st.markdown(stage_text(stages.get("detect", "WAITING"), "Moments"), unsafe_allow_html=True)
                    with c3:
                        st.markdown(stage_text(stages.get("selection", "WAITING"), "Review"), unsafe_allow_html=True)
                    with c4:
                        st.markdown(stage_text(stages.get("transcribe", "WAITING"), "Transcribe"), unsafe_allow_html=True)
                    with c5:
                        st.markdown(stage_text(stages.get("render", "WAITING"), "Render"), unsafe_allow_html=True)

                # Review-before-render moment list
                if status == "AWAITING_SELECTION":
                    moments = job.get("detected_moments", [])
                    st.markdown(f"**Found {len(moments)} hype moments. Review and select moments to render:**")

                    selected_indices = []
                    for m_idx, m in enumerate(moments):
                        m_start = m.get("start", 0)
                        m_end = m.get("end", 0)
                        m_dur = m_end - m_start
                        m_score = m.get("score", 0.0)
                        m_preview = m.get("preview", "")

                        # Format timestamp H:MM:SS
                        ts_start = f"{int(m_start//3600):02d}:{int((m_start%3600)//60):02d}:{int(m_start%60):02d}"
                        ts_end = f"{int(m_end//3600):02d}:{int((m_end%3600)//60):02d}:{int(m_end%60):02d}"

                        # Score breakdown
                        score_details = [f"Score: **{m_score:.2f}**"]
                        bd = m.get("breakdown", {})
                        if bd:
                            if "chat_rate" in bd:
                                score_details.append(f"Chat: +{bd['chat_rate']:.2f}")
                            if "chat_keywords" in bd:
                                score_details.append(f"Keywords: +{bd['chat_keywords']:.2f}")
                            if "streamer_loudness" in bd:
                                score_details.append(f"Loudness: +{bd['streamer_loudness']:.2f}")
                        if m.get("llm_score"):
                            score_details.append(f"LLM Judge: {m['llm_score']:.1f}/10")

                        breakdown_str = " | ".join(score_details)

                        col_chk, col_info = st.columns([1, 11])
                        with col_chk:
                            is_checked = st.checkbox(
                                f"#{m_idx+1}",
                                value=True,
                                key=f"chk_{jid}_{m_idx}",
                            )
                            if is_checked:
                                selected_indices.append(m_idx)
                        with col_info:
                            st.markdown(
                                f"**Moment #{m_idx+1}:** `{ts_start} - {ts_end}` ({int(m_dur)}s) | {breakdown_str}",
                                unsafe_allow_html=True,
                            )
                            # Editable hook title
                            cur_hook = m.get("hook_title") or (m_preview[:45] if m_preview else "")
                            new_hook = st.text_input(
                                "Hook Title",
                                value=cur_hook,
                                key=f"hook_{jid}_{m_idx}",
                                label_visibility="collapsed",
                            )
                            m["hook_title"] = new_hook

                            # LLM Judge reason if present
                            judge_reason = m.get("reason") or m.get("llm_reason")
                            if judge_reason:
                                st.caption(f"💡 *Judge Note:* {judge_reason}")
                            elif m_preview:
                                st.caption(f"Transcript: *\"{m_preview}\"*")

                    render_btn_label = f"Render Selected ({len(selected_indices)} clips)"
                    if st.button(render_btn_label, key=f"btn_render_{jid}", type="primary"):
                        if not selected_indices:
                            st.error("Please select at least one moment.")
                        else:
                            confirm_render_moments(jid, selected_indices, updated_moments=moments)
                            st.rerun()

                elif status == "COMPLETED":
                    st.caption(f"Completed ({len(job.get('clips', []))} clips created)")
                elif status == "FAILED":
                    st.caption(f"Error: {job.get('error')}")

                # Per-job live logs
                with st.expander("Logs", expanded=False):
                    job_lines = get_job_logs(jid, max_lines=30)
                    if job_lines:
                        st.code("".join(job_lines), language="text")
                    else:
                        st.caption("No log entries.")

                st.divider()

    render_queue_fragment()


# ==========================================
# TAB 4: CLIPS GALLERY & EXPORT
# ==========================================
with tab_gallery:
    st.markdown("### Clips Gallery")

    jobs = load_jobs()

    # Filter out test/synthetic clips unless the job came from the developer test tool
    def is_synthetic_job(j: dict) -> bool:
        vod = (j.get("vod_input") or "").lower()
        return "synthetic" in vod or "test_" in vod

    completed_jobs = {}
    for jid, j in jobs.items():
        if j.get("status") == "COMPLETED" and j.get("clips"):
            if is_synthetic_job(j):
                if j.get("is_dev_test"):
                    completed_jobs[jid] = j
            else:
                completed_jobs[jid] = j

    if not completed_jobs:
        st.info("No clips available. Completed clips will appear here.")
    else:
        selected_job_id = st.selectbox(
            "Select completed job",
            options=list(completed_jobs.keys()),
            format_func=lambda x: f"Job #{x} - {completed_jobs[x].get('vod_input', '')[:50]} ({len(completed_jobs[x].get('clips', []))} clips)"
            + (" [Developer Test]" if completed_jobs[x].get("is_dev_test") else ""),
        )

        job_data = completed_jobs[selected_job_id]
        campaign_info = job_data.get("campaign") or {}
        clips = job_data.get("clips", [])

        # Show campaign brief if present
        if campaign_info.get("rules_note"):
            st.caption(f"Campaign rules: {campaign_info.get('rules_note')}")

        st.caption(f"{len(clips)} clips in this job.")

        cols_per_row = 2
        for idx in range(0, len(clips), cols_per_row):
            row_clips = clips[idx : idx + cols_per_row]
            cols = st.columns(cols_per_row)

            for c_idx, clip in enumerate(row_clips):
                global_idx = idx + c_idx
                with cols[c_idx]:
                    st.markdown(f"**Clip #{clip.get('index', global_idx+1)}: {clip.get('filename')}**")

                    # QC Badges and Layout info
                    qc = clip.get("qc", {})
                    if qc:
                        qc_st = qc.get("status", "PASS")
                        lufs_val = qc.get("measured_lufs", -14.0)
                        flags = qc.get("flags", [])
                        flag_tooltip = " | ".join(flags) if flags else "All checks passed"

                        badge_map = {
                            "PASS": "<span class='badge-clean' title='All checks passed'>QC: PASS</span>",
                            "WARN": f"<span class='badge-paused' title='{flag_tooltip}'>QC: WARN</span>",
                            "FAIL": f"<span class='badge-flagged' title='{flag_tooltip}'>QC: FAIL</span>",
                        }
                        qc_badge = badge_map.get(qc_st, "<span class='badge-clean'>QC: PASS</span>")

                        st.markdown(
                            f"{qc_badge} | **{lufs_val:.1f} LUFS** | Layout: **{clip.get('layout', 'gameplay')}** | Status: **{clip.get('status', 'PENDING')}**",
                            unsafe_allow_html=True,
                        )
                        if flags:
                            for flg in flags:
                                st.caption(f"⚠️ *{flg}*")
                    else:
                        st.markdown(
                            f"Score: **{clip.get('hype_score', 0):.2f}** | Status: **{clip.get('status', 'PENDING')}**",
                            unsafe_allow_html=True,
                        )

                    # Video player
                    clip_path = clip.get("path")
                    if clip_path and os.path.isfile(clip_path):
                        st.video(clip_path)

                    # Hook Title (editable)
                    cur_hook = clip.get("hook_title") or clip.get("suggested_title", "")
                    new_hook = st.text_input(
                        "Hook Title",
                        value=cur_hook,
                        key=f"g_hook_{selected_job_id}_{global_idx}",
                    )
                    if new_hook != cur_hook:
                        clip["hook_title"] = new_hook
                        clip["suggested_title"] = new_hook
                        clip["platforms"] = generate_platform_metadata(
                            hook_title=new_hook,
                            transcript=clip.get("transcript", ""),
                            campaign=campaign_info,
                        )
                        jobs[selected_job_id]["clips"] = clips
                        save_jobs(jobs)

                    # Layout Selector & Re-render button
                    col_lay, col_re = st.columns([2, 1.2])
                    cur_lay = (clip.get("layout") or "gameplay").capitalize()
                    layout_opts = ["Gameplay", "Split", "Face"]
                    lay_idx = layout_opts.index(cur_lay) if cur_lay in layout_opts else 0

                    with col_lay:
                        chosen_lay = st.selectbox(
                            "Layout",
                            layout_opts,
                            index=lay_idx,
                            key=f"g_lay_{selected_job_id}_{global_idx}",
                        )
                    with col_re:
                        st.write("")
                        if st.button("Re-render", key=f"g_re_{selected_job_id}_{global_idx}", use_container_width=True):
                            clip_dir = os.path.dirname(clip["path"])
                            clip_idx = clip.get("index", global_idx + 1)
                            r_path = os.path.join(clip_dir, f"rough_{clip_idx:02d}.mp4")
                            a_path = os.path.join(clip_dir, f"cap_{clip_idx:02d}.ass")
                            if os.path.isfile(r_path):
                                with st.spinner(f"Re-rendering with {chosen_lay} layout..."):
                                    render_vertical_clip(
                                        r_path,
                                        a_path if os.path.isfile(a_path) else None,
                                        clip["path"],
                                        layout=chosen_lay.lower(),
                                        facecam_info=clip.get("facecam"),
                                        use_cpu=False,
                                    )
                                    clip["layout"] = chosen_lay.lower()
                                    clip["qc"] = perform_qc_check(
                                        clip["path"],
                                        duration=clip.get("duration", 30.0),
                                        campaign=campaign_info,
                                        facecam_info=clip.get("facecam"),
                                        word_count=clip.get("word_count", 0),
                                        layout=chosen_lay.lower(),
                                    )
                                    jobs[selected_job_id]["clips"] = clips
                                    save_jobs(jobs)
                                    st.success(f"Clip re-rendered as {chosen_lay}!")
                                    time.sleep(0.5)
                                    st.rerun()
                            else:
                                st.error("Source rough clip file not found for re-rendering.")

                    # Platform Captions with one-click copy blocks
                    platforms = clip.get("platforms")
                    if not platforms:
                        platforms = generate_platform_metadata(
                            hook_title=clip.get("hook_title") or clip.get("suggested_title", ""),
                            transcript=clip.get("transcript", ""),
                            campaign=campaign_info,
                        )

                    with st.expander("Platform Captions (One-Click Copy)", expanded=False):
                        p_col1, p_col2 = st.columns(2)
                        with p_col1:
                            st.markdown("**YouTube Shorts:**")
                            st.code(f"{platforms.get('youtube_title', '')}\n\n{platforms.get('youtube_description', '')}", language="text")
                            st.markdown("**TikTok:**")
                            st.code(platforms.get("tiktok_caption", ""), language="text")
                        with p_col2:
                            st.markdown("**Instagram Reels:**")
                            st.code(platforms.get("reels_caption", ""), language="text")
                            st.markdown("**X (Twitter):**")
                            st.code(platforms.get("x_tweet", ""), language="text")

                    # Action buttons in a row
                    b1, b2, b3 = st.columns(3)
                    with b1:
                        if st.button("Keep", key=f"g_keep_{selected_job_id}_{global_idx}", use_container_width=True):
                            clip["status"] = "KEPT"
                            jobs[selected_job_id]["clips"] = clips
                            save_jobs(jobs)
                            st.rerun()

                    with b2:
                        rename_val = st.text_input(
                            "Rename",
                            value=os.path.basename(clip["path"]),
                            key=f"g_ren_val_{selected_job_id}_{global_idx}",
                            label_visibility="collapsed",
                        )
                        if st.button("Rename", key=f"g_ren_btn_{selected_job_id}_{global_idx}", use_container_width=True):
                            if rename_val and rename_val != os.path.basename(clip["path"]):
                                new_full = os.path.join(os.path.dirname(clip["path"]), rename_val)
                                try:
                                    os.rename(clip["path"], new_full)
                                    clip["path"] = new_full
                                    clip["filename"] = rename_val
                                    jobs[selected_job_id]["clips"] = clips
                                    save_jobs(jobs)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Rename failed: {e}")

                    with b3:
                        if st.button("Delete", key=f"g_del_{selected_job_id}_{global_idx}", use_container_width=True):
                            clip["status"] = "DELETED"
                            jobs[selected_job_id]["clips"] = clips
                            save_jobs(jobs)
                            st.rerun()

                    st.divider()

        # Batch Export
        kept_clips = [c for c in clips if c.get("status") == "KEPT"]
        st.markdown(f"**Kept clips: {len(kept_clips)} of {len(clips)}**")

        if st.button("Export kept clips", type="primary"):
            if not kept_clips:
                st.warning("No clips marked as Kept.")
            else:
                camp_id = campaign_info.get("id") or "general"
                today_str = time.strftime("%Y-%m-%d")
                export_dir = os.path.abspath(os.path.join("exports", camp_id, today_str))
                os.makedirs(export_dir, exist_ok=True)

                exported_count = 0
                for kc in kept_clips:
                    src = kc.get("path")
                    if src and os.path.isfile(src):
                        dest = os.path.join(export_dir, os.path.basename(src))
                        shutil.copy2(src, dest)

                        base_name, _ = os.path.splitext(os.path.basename(src))
                        meta_file = os.path.join(export_dir, f"{base_name}_meta.txt")
                        with open(meta_file, "w", encoding="utf-8") as mf:
                            mf.write(f"TITLE:\n{kc.get('suggested_title', '')}\n\n")
                            mf.write(f"DESCRIPTION:\n{kc.get('suggested_description', '')}\n\n")
                            mf.write(f"HASHTAGS:\n{kc.get('suggested_hashtags', '')}\n")

                        exported_count += 1

                st.success(f"Exported {exported_count} clips to {export_dir}")
