#!/usr/bin/env python3
"""app.py — connectTools Streamlit GUI.

Run with:
    streamlit run app.py
or via the connectToolbox wrapper (coming soon).
"""

from __future__ import annotations

import configparser
import datetime as dt
import json
import sys
import time
from pathlib import Path

import boto3
import streamlit as st
from botocore.config import Config
from botocore.exceptions import ClientError

# ── Add toolbox dir to path so we can import sibling modules ──────────────────
sys.path.insert(0, str(Path(__file__).parent))
import contact_investigator as ci
import contact_search as cs
import ct_config as ctcfg
import log_insights as li

RETRY_CONFIG    = Config(retries={"max_attempts": 2, "mode": "standard"})
AWS_CREDS_FILE  = Path.home() / ".aws" / "credentials"
QUERIES_DIR     = Path(__file__).parent / "queries"
GUI_PROFILES_KEY = "gui_profiles"
LAST_PROFILE_KEY = "last_gui_profile"

st.set_page_config(
    page_title="connectTools",
    page_icon="🔗",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ══════════════════════════════════════════════════════════════════════════════
# Profile metadata (stored in ct_config alongside existing tool config)
# ══════════════════════════════════════════════════════════════════════════════

def _ct_load() -> dict:
    if ctcfg.CONFIG_FILE.exists():
        try:
            return json.loads(ctcfg.CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _ct_save(data: dict):
    ctcfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ctcfg.CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_profiles() -> dict:
    """Returns {profile_name: {display_name, instance_id, region, added_at}}"""
    return _ct_load().get(GUI_PROFILES_KEY, {})


def save_profile(profile_name: str, display_name: str, instance_id: str, region: str):
    cfg = _ct_load()
    cfg.setdefault(GUI_PROFILES_KEY, {})[profile_name] = {
        "display_name": display_name.strip() or profile_name,
        "instance_id":  instance_id.strip(),
        "region":       region.strip(),
        "added_at":     dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _ct_save(cfg)


def delete_profile_meta(profile_name: str):
    cfg = _ct_load()
    cfg.get(GUI_PROFILES_KEY, {}).pop(profile_name, None)
    if cfg.get(LAST_PROFILE_KEY) == profile_name:
        cfg.pop(LAST_PROFILE_KEY, None)
    _ct_save(cfg)


def get_last_profile() -> str:
    return _ct_load().get(LAST_PROFILE_KEY, "")


def set_last_profile(profile_name: str):
    cfg = _ct_load()
    cfg[LAST_PROFILE_KEY] = profile_name
    _ct_save(cfg)


# ══════════════════════════════════════════════════════════════════════════════
# AWS credentials file helpers
# ══════════════════════════════════════════════════════════════════════════════

def read_aws_creds() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if AWS_CREDS_FILE.exists():
        cp.read(str(AWS_CREDS_FILE), encoding="utf-8")
    return cp


def write_aws_creds(profile_name: str, key_id: str, secret: str, token: str):
    AWS_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cp = read_aws_creds()
    if profile_name not in cp:
        cp[profile_name] = {}
    cp[profile_name]["aws_access_key_id"]     = key_id
    cp[profile_name]["aws_secret_access_key"] = secret
    cp[profile_name]["aws_session_token"]     = token
    with open(AWS_CREDS_FILE, "w", encoding="utf-8") as f:
        cp.write(f)


def delete_aws_creds(profile_name: str):
    cp = read_aws_creds()
    if profile_name in cp:
        cp.remove_section(profile_name)
        with open(AWS_CREDS_FILE, "w", encoding="utf-8") as f:
            cp.write(f)


def parse_option2_block(text: str) -> tuple | None:
    """
    Parse an AWS IAM Identity Center Option 2 credential block.
    Returns (profile_name, key_id, secret, token) or None.
    """
    text = text.strip()
    if not text:
        return None
    # Add dummy header if user pasted just the key=value lines without [section]
    if not text.startswith("["):
        text = "[default]\n" + text
    try:
        cp = configparser.ConfigParser()
        cp.read_string(text)
        section = cp.sections()[0]
        key_id  = cp[section].get("aws_access_key_id", "").strip()
        secret  = cp[section].get("aws_secret_access_key", "").strip()
        token   = cp[section].get("aws_session_token", "").strip()
        if not key_id or not secret or not token:
            return None
        return section, key_id, secret, token
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# AWS helpers
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def check_profile(profile_name: str) -> tuple:
    """Returns (is_valid: bool, detail: str). Result cached 5 min."""
    try:
        session  = boto3.Session(profile_name=profile_name)
        sts      = session.client("sts", config=RETRY_CONFIG)
        identity = sts.get_caller_identity()
        return True, f"Account {identity['Account']} · {identity['Arn'].split('/')[-1]}"
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ExpiredTokenException", "InvalidClientTokenId", "AuthFailure"):
            return False, "Credentials expired — paste a fresh block to refresh"
        return False, e.response["Error"]["Message"]
    except Exception as e:
        return False, str(e)


def _status_badge(profile_name: str) -> str:
    """Return a colour-coded emoji status without blocking on an API call."""
    key = f"validity_{profile_name}"
    if key not in st.session_state:
        return "⚪"
    return "🟢" if st.session_state[key][0] else "🔴"


# ══════════════════════════════════════════════════════════════════════════════
# Sidebar
# ══════════════════════════════════════════════════════════════════════════════

def render_sidebar() -> tuple:
    """Render sidebar; returns (active_profile_name, profile_meta_dict)."""
    profiles = load_profiles()

    with st.sidebar:
        st.title("🔗 connectTools")
        st.divider()

        # ── Active profile selector ───────────────────────────────────────────
        if not profiles:
            st.warning("No profiles saved yet.\nGo to **Credentials** to add one.")
            active_name = ""
            active_meta = {}
        else:
            options      = list(profiles.keys())
            display_map  = {k: f"{_status_badge(k)}  {v['display_name']}" for k, v in profiles.items()}
            last         = get_last_profile()
            default_idx  = options.index(last) if last in options else 0

            selected = st.selectbox(
                "Active profile",
                options=options,
                index=default_idx,
                format_func=lambda k: display_map[k],
                key="active_profile_select",
            )
            if selected != st.session_state.get("_last_selected"):
                st.session_state["_last_selected"] = selected
                set_last_profile(selected)

            active_name = selected
            active_meta = profiles[selected]

            vkey  = f"validity_{active_name}"
            badge = _status_badge(active_name)

            with st.expander(f"{badge}  {active_meta['display_name']}", expanded=False):
                st.caption(
                    f"**Instance:** `{active_meta.get('instance_id') or '—'}`  \n"
                    f"**Region:** `{active_meta.get('region') or '—'}`"
                )
                if vkey in st.session_state:
                    valid, detail = st.session_state[vkey]
                    if valid:
                        st.success(detail, icon="✅")
                    else:
                        st.error(detail, icon="🔴")
                if st.button("Check credentials", key="check_creds", use_container_width=True):
                    check_profile.clear()
                    with st.spinner("Checking…"):
                        result = check_profile(active_name)
                    st.session_state[vkey] = result
                    st.rerun()

        st.divider()

        # ── Navigation ────────────────────────────────────────────────────────
        page = st.radio(
            "Navigate",
            ["🔑  Credentials", "🔎  Contact Search", "🔍  Contact Investigator",
             "📊  Log Insights"],
            key="nav",
            label_visibility="collapsed",
        )

    return active_name, active_meta, page.strip().lstrip("🔑🔎🔍📊 ")


# ══════════════════════════════════════════════════════════════════════════════
# Page: Credentials
# ══════════════════════════════════════════════════════════════════════════════

def page_credentials():
    st.header("🔑 Credentials")
    st.caption(
        "Paste the **Option 2** block from the AWS IAM Identity Center access portal "
        "to add or refresh a profile. Credentials are written to `~/.aws/credentials`; "
        "instance and region settings are stored in `~/.connecttools/config.json`."
    )

    # ── Add / Refresh form ────────────────────────────────────────────────────
    if "paste_key" not in st.session_state:
        st.session_state["paste_key"] = 0

    with st.expander("➕ Add or refresh credentials", expanded=not load_profiles()):
        st.markdown(
            "In the **AWS access portal**, click an account → click a role → "
            "select **Access keys** → copy the **Option 2** block and paste it below."
        )

        pasted = st.text_area(
            "Paste Option 2 credential block",
            height=140,
            placeholder=(
                "[123456789012_PowerAdmin]\n"
                "aws_access_key_id=ASIA...\n"
                "aws_secret_access_key=...\n"
                "aws_session_token=IQoJ..."
            ),
            key=f"paste_block_{st.session_state['paste_key']}",
        )

        parsed = parse_option2_block(pasted) if pasted.strip() else None
        if pasted.strip() and parsed is None:
            st.warning("Couldn't parse that block. Make sure you copied the full Option 2 text.")

        if parsed:
            profile_name, key_id, secret, token = parsed
            st.success(f"Parsed profile: **{profile_name}**  ·  Key ID: `{key_id[:8]}…`")

            profiles  = load_profiles()
            existing  = profiles.get(profile_name, {})
            is_update = profile_name in profiles

            col1, col2, col3 = st.columns(3)
            with col1:
                display_name = st.text_input(
                    "Display name",
                    value=existing.get("display_name", profile_name),
                    key="new_display",
                    help="Friendly name shown in the profile selector",
                )
            with col2:
                instance_id = st.text_input(
                    "Connect instance ID",
                    value=existing.get("instance_id", ""),
                    key="new_instance",
                    placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                )
            with col3:
                region = st.text_input(
                    "Region",
                    value=existing.get("region", "us-east-1"),
                    key="new_region",
                )

            label = "💾 Update profile" if is_update else "💾 Save profile"
            if st.button(label, type="primary", disabled=not instance_id.strip()):
                write_aws_creds(profile_name, key_id, secret, token)
                save_profile(profile_name, display_name, instance_id, region)
                st.session_state.pop(f"validity_{profile_name}", None)
                check_profile.clear()
                set_last_profile(profile_name)
                st.session_state["paste_key"] = st.session_state.get("paste_key", 0) + 1
                action = "updated" if is_update else "saved"
                st.success(f"Profile **{display_name or profile_name}** {action}.")
                st.rerun()
            elif not instance_id.strip():
                st.caption("Instance ID is required to save.")

    st.divider()

    # ── Saved profiles table ──────────────────────────────────────────────────
    profiles = load_profiles()
    if not profiles:
        st.info("No profiles saved yet. Add one above.")
        return

    st.subheader("Saved profiles")

    for profile_name, meta in profiles.items():
        display_name = meta.get("display_name", profile_name)
        instance_id  = meta.get("instance_id", "")
        region       = meta.get("region", "")
        added_at     = meta.get("added_at", "")
        vkey         = f"validity_{profile_name}"
        valid, vdetail = st.session_state.get(vkey, (None, ""))

        badge = ("🟢 Valid" if valid else ("🔴 Expired" if valid is False else "⚪ Unknown"))

        with st.expander(f"{badge}  **{display_name}**  ·  `{profile_name}`", expanded=False):
            info_col, action_col = st.columns([3, 1])
            with info_col:
                st.markdown(
                    f"**Instance ID:** `{instance_id or '—'}`  \n"
                    f"**Region:** `{region or '—'}`  \n"
                    f"**Added:** {added_at[:10] if added_at else '—'}  \n"
                    f"**Credential detail:** {vdetail or '—'}"
                )
            with action_col:
                if st.button("Check", key=f"chk_{profile_name}"):
                    check_profile.clear()
                    with st.spinner("Checking…"):
                        result = check_profile(profile_name)
                    st.session_state[vkey] = result
                    st.rerun()
                if st.button("🗑 Delete", key=f"del_{profile_name}"):
                    st.session_state[f"confirm_delete_{profile_name}"] = True

            if st.session_state.get(f"confirm_delete_{profile_name}"):
                st.warning(f"Delete profile **{display_name}**? This removes it from `~/.aws/credentials`.")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Yes, delete", key=f"yes_del_{profile_name}", type="primary"):
                        delete_aws_creds(profile_name)
                        delete_profile_meta(profile_name)
                        st.session_state.pop(f"confirm_delete_{profile_name}", None)
                        st.session_state.pop(vkey, None)
                        st.rerun()
                with c2:
                    if st.button("Cancel", key=f"no_del_{profile_name}"):
                        st.session_state.pop(f"confirm_delete_{profile_name}", None)
                        st.rerun()

            st.divider()

            # ── Edit form ─────────────────────────────────────────────────────
            with st.form(key=f"edit_{profile_name}"):
                st.caption("Edit settings")
                ec1, ec2, ec3 = st.columns(3)
                with ec1:
                    new_dn  = st.text_input("Display name",   value=display_name)
                with ec2:
                    new_iid = st.text_input("Instance ID",    value=instance_id)
                with ec3:
                    new_reg = st.text_input("Region",         value=region)
                if st.form_submit_button("Save changes"):
                    save_profile(profile_name, new_dn, new_iid, new_reg)
                    st.success("Saved.")
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Page: Contact Investigator
# ══════════════════════════════════════════════════════════════════════════════

def _render_overview(data: dict):
    contact = data.get("contact", {})
    names   = data.get("names", {})

    rows = {
        "Channel":           contact.get("Channel"),
        "Initiation method": contact.get("InitiationMethod"),
        "Initiated":         _fmt_ts(contact.get("InitiationTimestamp")),
        "Disconnected":      _fmt_ts(contact.get("DisconnectTimestamp")),
        "Disconnect reason": contact.get("DisconnectReason"),
        "Duration":          _fmt_dur(contact.get("InitiationTimestamp"),
                                       contact.get("DisconnectTimestamp")),
        "Queue":             names.get("queue") or (contact.get("QueueInfo") or {}).get("Id"),
        "Agent":             names.get("agent") or (contact.get("AgentInfo") or {}).get("Id"),
        "Customer endpoint": _endpoint_str(contact.get("CustomerEndpoint")),
        "Previous contact":  contact.get("PreviousContactId"),
    }
    rows = {k: v for k, v in rows.items() if v}

    col1, col2 = st.columns(2)
    items = list(rows.items())
    half  = (len(items) + 1) // 2
    with col1:
        for k, v in items[:half]:
            st.markdown(f"**{k}:** {v}")
    with col2:
        for k, v in items[half:]:
            st.markdown(f"**{k}:** {v}")

    chain = data.get("transfer_chain", [])
    if chain:
        st.divider()
        st.caption("Transfer chain (oldest → current)")
        for c in chain:
            st.markdown(f"→ `{c.get('Id', '?')[:8]}…`  {c.get('Channel', '')}  "
                        f"{_fmt_ts(c.get('InitiationTimestamp'))}")
        st.markdown(f"→ **current** `{contact.get('Id', '')}`")

    attrs = data.get("attributes", {})
    if attrs and "_error" not in attrs:
        st.divider()
        st.caption("Contact attributes")
        st.dataframe(
            [{"Key": k, "Value": v} for k, v in sorted(attrs.items())],
            use_container_width=True, hide_index=True,
        )

    lens = data.get("contact_lens", {})
    if "skipped" in lens:
        st.caption(f"Contact Lens: {lens['skipped']}")
    elif "error" in lens:
        st.caption(f"Contact Lens unavailable: {lens['error']}")
    elif "segments" in lens:
        segs        = lens["segments"]
        transcripts = [s["Transcript"] for s in segs if "Transcript" in s]
        categories  = [s["Categories"]  for s in segs if "Categories"  in s]
        st.divider()
        st.caption(f"Contact Lens · {len(transcripts)} transcript turn(s)")
        for cat in categories:
            matched = cat.get("MatchedCategories", [])
            if matched:
                st.markdown("**Categories:** " + "  ·  ".join(matched))


def _render_timeline(data: dict):
    events = data.get("events", [])
    if not events:
        st.info("No events.")
        return

    st.caption(
        f"{data.get('event_count', 0)} events  ·  "
        f"log group: `{data.get('log_group', '—')}`"
    )

    import pandas as pd
    df = pd.DataFrame([
        {
            "Offset":  e["offset_fmt"],
            "Kind":    e["kind"],
            "Event":   e["label"],
            "Detail":  e["detail"],
        }
        for e in events
    ])

    def _row_style(row):
        if row["Kind"] == "CONTACT":
            return ["font-weight:bold"] * len(row)
        if row["Kind"] == "LAMBDA":
            return ["color:#b45309"] * len(row)
        if row["Kind"] == "LENS":
            return ["color:#6b7280"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df.style.apply(_row_style, axis=1),
        use_container_width=True,
        hide_index=True,
        height=min(600, 35 + 35 * len(df)),
    )


def _render_lambda(data: dict):
    invocations = data.get("invocations", [])
    if not invocations:
        st.info("No Lambda invocations found in flow logs.")
        return
    for i, inv in enumerate(invocations, 1):
        result_icon = "✅" if inv["result"] == "Success" else "❌"
        with st.expander(
            f"{result_icon} [{i}] **{inv['function_name']}** · {inv['result']}",
            expanded=(inv["result"] != "Success"),
        ):
            st.markdown(
                f"**ARN:** `{inv['function_arn']}`  \n"
                f"**Invoked:** {inv['invoked_at'][:19].replace('T', ' ')} UTC  \n"
                f"**Flow:** {inv.get('flow_name') or '—'}  \n"
                f"**Result:** {inv['result']}"
            )
            if inv.get("connect_response"):
                st.caption("Connect-side response")
                st.json(inv["connect_response"], expanded=False)
            if inv.get("lambda_logs"):
                st.caption(f"Lambda CloudWatch logs ({len(inv['lambda_logs'])} lines)")
                log_text = "\n".join(
                    f"{e['timestamp'][11:23]}  {e['message']}"
                    for e in inv["lambda_logs"]
                )
                st.code(log_text, language=None)
            elif data.get("lambda_logs_fetched") is False:
                st.caption("CloudWatch logs not fetched — enable **Lambda logs** to see them.")


def _render_recordings(data: dict):
    artifacts = data.get("artifacts", {})
    channel   = data.get("channel", "")
    expires   = data.get("url_expires_seconds", 3600)

    def _show_group(label, items):
        if not items:
            st.caption(f"{label}: none found")
            return
        st.markdown(f"**{label}**")
        for item in items:
            subtype = item.get("subtype", "original").upper()
            s3_uri  = item.get("s3_uri", "")
            url     = item.get("presigned_url")
            col1, col2 = st.columns([2, 1])
            with col1:
                st.code(s3_uri, language=None)
            with col2:
                if url:
                    st.link_button(f"⬇ Download ({subtype})", url)
                else:
                    st.caption("Presign failed")

    st.caption(f"Channel: {channel}  ·  Date: {data.get('date', '—')}  ·  URLs expire: {expires // 60}m")
    if channel == "VOICE" or not channel:
        _show_group("Recordings",            artifacts.get("recordings", []))
        _show_group("Contact Lens Analysis", artifacts.get("analysis", []))
    elif channel == "CHAT":
        _show_group("Chat Transcripts",      artifacts.get("transcripts", []))
        _show_group("Contact Lens Analysis", artifacts.get("analysis", []))
    else:
        for name, items in artifacts.items():
            _show_group(name.replace("_", " ").title(), items)


def page_contact_investigator(active_name: str, active_meta: dict):
    st.header("🔍 Contact Investigator")

    if not active_name:
        st.warning("Select or add a profile in the sidebar first.")
        return

    instance_id = active_meta.get("instance_id", "")
    region      = active_meta.get("region", "us-east-1")

    # ── Input form ────────────────────────────────────────────────────────────
    with st.form("investigate_form"):
        col1, col2 = st.columns([3, 1])
        with col1:
            contact_id = st.text_input(
                "Contact ID",
                value=st.session_state.pop("prefill_contact_id", ""),
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                help="The Amazon Connect contact UUID",
            )
        with col2:
            instance_override = st.text_input(
                "Instance ID",
                value=instance_id,
                help="Override the instance ID from the active profile",
            )

        st.caption("Sections")
        sc1, sc2, sc3, sc4, sc5 = st.columns(5)
        with sc1: do_overview    = st.checkbox("Overview",    value=True)
        with sc2: do_timeline    = st.checkbox("Timeline",    value=True)
        with sc3: do_lambda      = st.checkbox("Lambda",      value=False)
        with sc4: do_recordings  = st.checkbox("Recordings",  value=False)
        with sc5: do_logs        = st.checkbox("Logs",        value=False)

        with st.expander("Advanced"):
            ac1, ac2, ac3 = st.columns(3)
            with ac1: transcript   = st.checkbox("Include Lens transcript")
            with ac2: lambda_logs  = st.checkbox("Fetch Lambda CW logs")
            with ac3: url_expires  = st.number_input("URL expiry (s)", value=3600, min_value=60, step=900)
            lg_col, save_col = st.columns([4, 1])
            with lg_col:
                log_group = st.text_input(
                    "Log group override",
                    value=ctcfg.get_log_group(instance_override or instance_id),
                    placeholder="/aws/connect/<alias>",
                )
            with save_col:
                save_log_group = st.checkbox(
                    "Save as default",
                    help="Save this log group as the default for this instance",
                )

        submitted = st.form_submit_button("Investigate", type="primary")

    if not submitted:
        return

    iid = (instance_override or instance_id).strip()
    cid = contact_id.strip()

    if not iid:
        st.error("Instance ID is required. Set it on the active profile or enter it above.")
        return
    if not cid:
        st.error("Contact ID is required.")
        return

    need_logs = do_timeline or do_lambda or do_logs
    need_s3   = do_recordings

    # ── Run investigation ─────────────────────────────────────────────────────
    with st.spinner("Fetching contact…"):
        try:
            connect, logs_client, s3_client = ci.make_clients(
                region, active_name, need_logs=need_logs, need_s3=need_s3
            )
        except SystemExit:
            st.error("Could not create AWS clients. Check region and credentials.")
            return

        try:
            contact = ci.fetch_contact(connect, iid, cid)
        except SystemExit:
            st.error(f"Contact `{cid}` not found or access denied.")
            return

    start_ts = contact.get("InitiationTimestamp")
    end_ts   = contact.get("DisconnectTimestamp")

    names = {}
    if do_overview or do_timeline:
        names = ci.resolve_names(connect, iid, contact)

    # Log group
    log_group_resolved = None
    if need_logs:
        try:
            log_group_resolved = ci.resolve_log_group(
                connect, iid, log_group.strip() or None
            )
        except SystemExit:
            st.error("Could not resolve Connect log group. Set it in Advanced options.")
            return
        if save_log_group and log_group_resolved:
            cfg = ctcfg.load()
            ctcfg.set_log_group(cfg, iid, log_group_resolved)
            st.toast(f"Log group saved as default for this instance.", icon="✅")

    # Fetch CW events once
    cw_events = []
    if need_logs and log_group_resolved and start_ts:
        with st.spinner("Fetching flow logs…"):
            import datetime as _dt
            now      = _dt.datetime.now(_dt.timezone.utc)
            start_ms = ci._ms(start_ts - _dt.timedelta(minutes=2))
            end_ms   = ci._ms(min(end_ts + _dt.timedelta(minutes=5), now) if end_ts else now)
            cw_events = ci.filter_log_events(
                logs_client, log_group_resolved,
                f'{{ $.ContactId = "{cid}" }}',
                start_ms, end_ms,
            )

    # Shared Lens cache
    lens_cache: dict = {}

    # ── Render sections in tabs ───────────────────────────────────────────────
    active_sections = [s for s, flag in [
        ("Overview",   do_overview),
        ("Timeline",   do_timeline),
        ("Lambda",     do_lambda),
        ("Recordings", do_recordings),
        ("Logs",       do_logs),
    ] if flag]

    if not active_sections:
        st.info("No sections selected.")
        return

    tabs = st.tabs(active_sections)

    for tab, section in zip(tabs, active_sections):
        with tab:
            if section == "Overview":
                with st.spinner("Loading overview…"):
                    data = ci.run_overview(
                        connect, iid, cid, contact,
                        lens_cache, names, transcript, output_json=True,
                    )
                _render_overview(data)

            elif section == "Timeline":
                with st.spinner("Building timeline…"):
                    data = ci.run_timeline(
                        connect, logs_client, iid, cid, contact,
                        log_group_resolved, cw_events, lens_cache,
                        names, transcript, output_json=True,
                    )
                _render_timeline(data)

            elif section == "Lambda":
                with st.spinner("Tracing Lambda invocations…"):
                    data = ci.run_lambda(
                        logs_client, cid, cw_events,
                        lambda_logs, output_json=True,
                    )
                _render_lambda(data)

            elif section == "Recordings":
                with st.spinner("Locating recordings…"):
                    data = ci.run_recordings(
                        connect, s3_client, iid, contact,
                        url_expires, output_json=True,
                    )
                _render_recordings(data)

            elif section == "Logs":
                with st.spinner("Processing log events…"):
                    data = ci.run_logs(cw_events, cid, contact,
                                       log_group_resolved, output_json=True)
                st.caption(
                    f"{data.get('event_count', 0)} events  ·  "
                    f"log group: `{data.get('log_group', '—')}`"
                )
                if data.get("events"):
                    st.download_button(
                        "⬇ Download logs JSON",
                        data=json.dumps(data, indent=2, default=ci._serial),
                        file_name=f"{cid}_logs.json",
                        mime="application/json",
                    )
                    with st.expander("Preview (first 50 events)"):
                        st.json({"events": data["events"][:50]}, expanded=False)
                else:
                    st.info("No flow log events found for this contact.")


# ══════════════════════════════════════════════════════════════════════════════
# Formatting helpers
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_ts(ts) -> str:
    if ts is None:
        return ""
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(ts)


def _fmt_dur(start, end) -> str:
    if not start or not end:
        return ""
    secs = int((end - start).total_seconds())
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s" if m else f"{s}s"


def _endpoint_str(ep) -> str:
    if not ep:
        return ""
    return f"{ep.get('Address', '')} ({ep.get('Type', '')})"


# ══════════════════════════════════════════════════════════════════════════════
# Page: Contact Search
# ══════════════════════════════════════════════════════════════════════════════

def page_contact_search(active_name: str, active_meta: dict):
    st.header("🔎 Contact Search")

    if not active_name:
        st.warning("Select or add a profile in the sidebar first.")
        return

    instance_id = active_meta.get("instance_id", "")
    region      = active_meta.get("region", "us-east-1")

    if not instance_id:
        st.warning("No instance ID set on this profile. Edit it in Credentials.")
        return

    # ── Search form ───────────────────────────────────────────────────────────
    with st.form("search_form"):
        dc1, dc2 = st.columns(2)
        with dc1:
            start_date = st.date_input(
                "Start date",
                value=dt.date.today() - dt.timedelta(days=1),
            )
        with dc2:
            end_date = st.date_input(
                "End date",
                value=dt.date.today(),
            )

        with st.expander("Filters"):
            fc1, fc2 = st.columns(2)
            with fc1:
                channels = st.multiselect(
                    "Channel",
                    ["VOICE", "CHAT", "TASK", "EMAIL"],
                )
            with fc2:
                methods = st.multiselect(
                    "Initiation method",
                    ["INBOUND", "OUTBOUND", "TRANSFER", "CALLBACK", "API",
                     "QUEUE_TRANSFER", "EXTERNAL_OUTBOUND", "MONITOR", "DISCONNECT"],
                )
            queue_id = st.text_input("Queue ID", placeholder="optional")

        limit = st.number_input(
            "Max results",
            min_value=1, max_value=500, value=100,
            help="SearchContacts is throttled at 0.5 TPS — large limits take time",
        )
        submitted = st.form_submit_button("Search", type="primary")

    if not submitted:
        return

    if start_date > end_date:
        st.error("Start date must be on or before end date.")
        return

    # Build criteria and time range
    criteria: dict = {}
    if channels:  criteria["Channels"]          = channels
    if methods:   criteria["InitiationMethods"] = methods
    if queue_id.strip():
        criteria["QueueIds"] = [queue_id.strip()]

    start_dt = dt.datetime.combine(start_date, dt.time.min,    tzinfo=dt.timezone.utc)
    end_dt   = dt.datetime.combine(end_date,   dt.time(23, 59, 59), tzinfo=dt.timezone.utc)
    time_range = {"Type": "INITIATION_TIMESTAMP", "StartTime": start_dt, "EndTime": end_dt}
    sort       = {"FieldName": "INITIATION_TIMESTAMP", "Order": "DESCENDING"}

    try:
        session = boto3.Session(profile_name=active_name)
        client  = session.client("connect", region_name=region, config=RETRY_CONFIG)
    except Exception as e:
        st.error(f"Could not create AWS client: {e}")
        return

    with st.spinner("Searching contacts (0.5 TPS limit — may take a moment)…"):
        try:
            contacts = cs.search_contacts(
                client, instance_id, time_range, criteria, sort, int(limit)
            )
        except SystemExit:
            st.error("Search failed. Check credentials and `connect:SearchContacts` permission.")
            return

    if not contacts:
        st.info("No contacts found for this time range and filters.")
        return

    # ── Build display table ───────────────────────────────────────────────────
    import pandas as pd

    rows = []
    for c in contacts:
        init_ts = c.get("InitiationTimestamp")
        disc_ts = c.get("DisconnectTimestamp")
        dur_s   = int((disc_ts - init_ts).total_seconds()) if init_ts and disc_ts else None
        qi      = c.get("QueueInfo") or {}
        rows.append({
            "Contact ID":  c.get("Id", ""),
            "Channel":     c.get("Channel", ""),
            "Method":      c.get("InitiationMethod", ""),
            "Initiated":   init_ts.strftime("%Y-%m-%d %H:%M:%S") if init_ts else "",
            "Duration":    f"{dur_s // 60}m {dur_s % 60}s" if dur_s is not None else "",
            "Disconnect":  c.get("DisconnectReason", ""),
            "Queue":       qi.get("Id", ""),
        })

    df = pd.DataFrame(rows)
    st.caption(f"{len(contacts)} contact(s) — click a row then Investigate")

    event = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Contact ID": st.column_config.TextColumn(width="medium"),
            "Queue":      st.column_config.TextColumn(width="medium"),
        },
    )

    selected = (event.selection.rows or []) if event and hasattr(event, "selection") else []
    if selected:
        cid = rows[selected[0]]["Contact ID"]
        st.info(f"Selected: `{cid}`")
        if st.button("🔍 Investigate this contact →", type="primary"):
            st.session_state["prefill_contact_id"] = cid
            st.session_state["nav"] = "🔍  Contact Investigator"
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Page: Log Insights
# ══════════════════════════════════════════════════════════════════════════════

def _list_queries() -> list[Path]:
    QUERIES_DIR.mkdir(exist_ok=True)
    return sorted(QUERIES_DIR.glob("*.sql")) + sorted(QUERIES_DIR.glob("*.txt"))


def page_log_insights(active_name: str, active_meta: dict):
    st.header("📊 Log Insights")

    if not active_name:
        st.warning("Select or add a profile in the sidebar first.")
        return

    region = active_meta.get("region", "us-east-1")

    # ── Query selector + editor ───────────────────────────────────────────────
    queries    = _list_queries()
    NEW_LABEL  = "— New query —"
    options    = [NEW_LABEL] + [q.name for q in queries]

    selected = st.selectbox("Saved query", options, key="li_selected")

    # Reset editor text when selection changes
    if selected != st.session_state.get("li_last_selected"):
        st.session_state["li_last_selected"] = selected
        st.session_state["li_editor_key"]    = st.session_state.get("li_editor_key", 0) + 1
        st.session_state["li_default_text"]  = (
            (QUERIES_DIR / selected).read_text(encoding="utf-8")
            if selected != NEW_LABEL else ""
        )

    query_text = st.text_area(
        "Query",
        value=st.session_state.get("li_default_text", ""),
        height=200,
        key=f"li_editor_{st.session_state.get('li_editor_key', 0)}",
        placeholder=(
            "fields @timestamp, ContactId, ContactFlowName, ContactFlowModuleType\n"
            "| filter ContactId = '{ CID }'\n"
            "| sort @timestamp asc"
        ),
    )

    # Save / Save As
    s1, s2 = st.columns([1, 1])
    with s1:
        if st.button(
            "💾 Save", key="li_save",
            disabled=(selected == NEW_LABEL or not query_text.strip()),
            help="Overwrite the selected query file",
        ):
            (QUERIES_DIR / selected).write_text(query_text, encoding="utf-8")
            st.toast(f"Saved {selected}", icon="✅")

    with s2:
        with st.popover("💾 Save as…", disabled=not query_text.strip()):
            new_name = st.text_input("Filename", placeholder="my_query.sql", key="li_new_name")
            if st.button("Save", key="li_confirm_save_as", disabled=not new_name.strip()):
                fname = new_name.strip()
                if not fname.endswith((".sql", ".txt")):
                    fname += ".sql"
                (QUERIES_DIR / fname).write_text(query_text, encoding="utf-8")
                st.session_state["li_last_selected"] = None  # force reload of list
                st.toast(f"Saved as {fname}", icon="✅")
                st.rerun()

    # ── Placeholder detection ─────────────────────────────────────────────────
    placeholders = list(dict.fromkeys(li._PLACEHOLDER_RE.findall(query_text))) if query_text else []
    var_values: dict[str, str] = {}
    if placeholders:
        st.caption("Placeholders — fill in values before running:")
        ph_cols = st.columns(min(len(placeholders), 4))
        for i, ph in enumerate(placeholders):
            with ph_cols[i % len(ph_cols)]:
                var_values[ph] = st.text_input(
                    ph, key=f"li_var_{ph}",
                    help=f"Replaces {{{ph}}} in the query",
                )

    st.divider()

    # ── Run settings ──────────────────────────────────────────────────────────
    # Key counter lets us force-reset the text_input when Discover sets a value
    if "li_lg_key" not in st.session_state:
        st.session_state["li_lg_key"] = 0

    saved_lg = ctcfg.get_log_group(active_meta.get("instance_id", ""))

    lg_col, disc_col, def_col = st.columns([4, 1, 1])
    with lg_col:
        log_group = st.text_input(
            "Log group",
            value=st.session_state.get("li_log_group_val", saved_lg),
            placeholder="/aws/connect/<alias>",
            key=f"li_log_group_{st.session_state['li_lg_key']}",
        )
    with disc_col:
        st.write("")
        with st.popover("Discover"):
            st.caption("Auto-discover /aws/connect/* log groups")
            if st.button("List log groups", key="li_do_discover"):
                try:
                    sess   = boto3.Session(profile_name=active_name)
                    lc     = sess.client("logs", region_name=region, config=RETRY_CONFIG)
                    groups = li.list_connect_log_groups(lc)
                    st.session_state["li_discovered"] = groups
                except Exception as e:
                    st.error(str(e))
            if st.session_state.get("li_discovered"):
                picked = st.radio(
                    "Select", st.session_state["li_discovered"], key="li_pick_group",
                )
                if st.button("Use this group", key="li_use_group"):
                    st.session_state["li_log_group_val"] = picked
                    st.session_state["li_lg_key"]       += 1
                    st.session_state["li_discovered"]    = []
                    st.rerun()
    with def_col:
        st.write("")
        if st.button(
            "Set default", key="li_set_default",
            disabled=not log_group.strip(),
            help="Save this log group as the default for the active instance",
        ):
            iid = active_meta.get("instance_id", "")
            if iid:
                cfg = ctcfg.load()
                ctcfg.set_log_group(cfg, iid, log_group.strip())
                st.toast("Log group saved as default.", icon="✅")
            else:
                st.warning("No instance ID on this profile — can't save a default.")

    tr_type = st.radio(
        "Time range", ["Relative", "Date range"],
        horizontal=True, key="li_tr_type",
    )

    if tr_type == "Relative":
        rc1, rc2 = st.columns([1, 2])
        with rc1:
            dur_n = st.number_input("Amount", min_value=1, value=24, key="li_dur_n")
        with rc2:
            dur_unit = st.selectbox("Unit", ["hours", "days", "minutes", "weeks"], key="li_dur_unit")
        unit_map = {"hours": "h", "days": "d", "minutes": "m", "weeks": "w"}
        delta    = li.parse_duration(f"{int(dur_n)}{unit_map[dur_unit]}")
        now      = dt.datetime.now(dt.timezone.utc)
        start_ts = int((now - delta).timestamp())
        end_ts   = int(now.timestamp())
    else:
        dc1, dc2 = st.columns(2)
        with dc1:
            sd = st.date_input("Start", value=dt.date.today() - dt.timedelta(days=1), key="li_sd")
        with dc2:
            ed = st.date_input("End",   value=dt.date.today(),                         key="li_ed")
        start_ts = int(dt.datetime.combine(sd, dt.time.min,         tzinfo=dt.timezone.utc).timestamp())
        end_ts   = int(dt.datetime.combine(ed, dt.time(23, 59, 59), tzinfo=dt.timezone.utc).timestamp())

    limit = st.number_input("Max rows", min_value=1, max_value=10000, value=1000, key="li_limit")

    unfilled = [p for p in placeholders if not var_values.get(p, "").strip()]
    run_ok   = bool(query_text.strip()) and bool(log_group.strip()) and not unfilled

    if not run_ok and unfilled:
        st.caption(f"Fill in placeholder(s) to run: {', '.join(unfilled)}")

    if not st.button("▶ Run query", type="primary", disabled=not run_ok):
        return

    # ── Resolve placeholders and run ──────────────────────────────────────────
    resolved = li._PLACEHOLDER_RE.sub(
        lambda m: var_values.get(m.group(1), m.group(0)), query_text
    )

    try:
        sess        = boto3.Session(profile_name=active_name)
        logs_client = sess.client("logs", region_name=region, config=RETRY_CONFIG)
        query_id    = li.start_query(
            logs_client, log_group.strip(), resolved, start_ts, end_ts, int(limit)
        )
    except SystemExit:
        st.error("Failed to start query. Check IAM permissions (logs:StartQuery, logs:GetQueryResults).")
        return
    except Exception as e:
        st.error(str(e))
        return

    status_slot = st.empty()
    with st.spinner("Running query…"):
        while True:
            resp   = logs_client.get_query_results(queryId=query_id)
            status = resp["status"]
            status_slot.caption(f"Status: **{status}**")
            if status == "Complete":
                status_slot.empty()
                break
            if status in ("Failed", "Cancelled", "Timeout"):
                st.error(f"Query ended with status: {status}")
                return
            time.sleep(2)

    results = resp.get("results", [])
    stats   = resp.get("statistics", {})
    headers, rows_data = li.flatten(results)

    if not rows_data:
        st.info("Query returned no results.")
        return

    matched = int(stats.get("recordsMatched", 0))
    scanned = int(stats.get("recordsScanned", 0))
    st.caption(
        f"**{len(rows_data):,}** row(s)  ·  "
        f"{matched:,} matched  ·  {scanned:,} scanned"
    )

    import pandas as pd
    df = pd.DataFrame(rows_data, columns=headers)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Download Excel
    try:
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = tmp.name
        li.export_excel(headers, rows_data, tmp_path)
        with open(tmp_path, "rb") as f:
            xlsx_bytes = f.read()
        os.unlink(tmp_path)
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        st.download_button(
            "⬇ Download Excel",
            data=xlsx_bytes,
            file_name=f"insights_{ts}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except ImportError:
        st.caption("Install `openpyxl` to enable Excel export.")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    active_name, active_meta, page = render_sidebar()

    if "Credentials" in page:
        page_credentials()
    elif "Contact Search" in page:
        page_contact_search(active_name, active_meta)
    elif "Contact Investigator" in page:
        page_contact_investigator(active_name, active_meta)
    elif "Log Insights" in page:
        page_log_insights(active_name, active_meta)


if __name__ == "__main__":
    main()
