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
import subprocess
import sys
import time
from pathlib import Path

import boto3
import streamlit as st
from botocore.config import Config
from botocore.exceptions import ClientError

# ── Add lib and flowSim dirs to path so we can import shared modules ────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import contact_investigator as ci
import contact_diff as cd
import contact_search as cs
import ct_config as ctcfg
import flow_analyze as fa
import lambda_errors as le
import log_insights as li

RETRY_CONFIG    = Config(retries={"max_attempts": 2, "mode": "standard"})
AWS_CREDS_FILE  = Path.home() / ".aws" / "credentials"
QUERIES_DIR     = Path(__file__).parent / "queries"
FLOWSIM_DIR     = Path(__file__).parent.parent / "flowSim"

sys.path.insert(0, str(FLOWSIM_DIR))
import replay_contact as rc
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
    """Returns {profile_name: {display_name, instance_id, region, log_group, added_at}}"""
    return _ct_load().get(GUI_PROFILES_KEY, {})


def save_profile(profile_name: str, display_name: str, instance_id: str,
                 region: str, log_group: str = ""):
    cfg      = _ct_load()
    existing = cfg.get(GUI_PROFILES_KEY, {}).get(profile_name, {})
    cfg.setdefault(GUI_PROFILES_KEY, {})[profile_name] = {
        "display_name": display_name.strip() or profile_name,
        "instance_id":  instance_id.strip(),
        "region":       region.strip(),
        "log_group":    log_group.strip() or existing.get("log_group", ""),
        "added_at":     existing.get("added_at") or dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _ct_save(cfg)


def get_profile_log_group(profile_name: str, instance_id: str) -> str:
    """Return log group for this profile; fall back to per-instance ct_config entry."""
    profile_lg = load_profiles().get(profile_name, {}).get("log_group", "")
    return profile_lg or ctcfg.get_log_group(instance_id)


def set_profile_log_group(profile_name: str, instance_id: str, log_group: str):
    """Save log group to the profile and to ct_config (for CLI tool compatibility)."""
    lg = log_group.strip()
    cfg = _ct_load()
    if profile_name in cfg.get(GUI_PROFILES_KEY, {}):
        cfg[GUI_PROFILES_KEY][profile_name]["log_group"] = lg
        _ct_save(cfg)
    if instance_id and lg:
        cfg2 = ctcfg.load()
        ctcfg.set_log_group(cfg2, instance_id, lg)


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
# HTML to PNG conversion
# ══════════════════════════════════════════════════════════════════════════════

def html_to_png(html_content: str, png_path: Path, tab_selector: str = None) -> bool:
    print(f"[PNG] Starting PNG generation for {png_path}", file=sys.stderr)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[PNG] ERROR: Playwright not installed", file=sys.stderr)
        return False

    try:
        print(f"[PNG] Launching Playwright browser...", file=sys.stderr)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            print(f"[PNG] Browser launched, creating page...", file=sys.stderr)
            page = browser.new_page(viewport={"width": 1400, "height": 900})

            print(f"[PNG] Setting HTML content ({len(html_content)} bytes)...", file=sys.stderr)
            page.set_content(html_content)

            # Wait for networkidle, then give Cytoscape time to render
            print(f"[PNG] Waiting for networkidle...", file=sys.stderr)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
                print(f"[PNG] Networkidle achieved", file=sys.stderr)
            except Exception as e:
                print(f"[PNG] Networkidle timeout (continuing): {e}", file=sys.stderr)

            print(f"[PNG] Waiting 2s for JS rendering...", file=sys.stderr)
            page.wait_for_timeout(2000)

            # Click tab if selector provided
            if tab_selector:
                print(f"[PNG] Clicking tab: {tab_selector}", file=sys.stderr)
                page.click(tab_selector)
                page.wait_for_timeout(500)

            print(f"[PNG] Taking screenshot to {png_path}...", file=sys.stderr)
            page.screenshot(path=str(png_path), full_page=True)
            print(f"[PNG] Screenshot saved, closing browser", file=sys.stderr)
            browser.close()

        print(f"[PNG] SUCCESS: {png_path} created ({png_path.stat().st_size} bytes)", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[PNG] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return False


def html_export_all_tabs(html_content: str, zip_path: Path) -> bool:
    print(f"[ZIP] Starting tab export to {zip_path}", file=sys.stderr)

    try:
        from playwright.sync_api import sync_playwright
        import zipfile
        print(f"[ZIP] Imports OK", file=sys.stderr)
    except ImportError as e:
        print(f"[ZIP] ERROR: Import failed: {e}", file=sys.stderr)
        return False

    try:
        print(f"[ZIP] Launching Playwright...", file=sys.stderr)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            print(f"[ZIP] Browser ready, setting content...", file=sys.stderr)
            page.set_content(html_content)

            # Wait for networkidle, then give Cytoscape time to render
            print(f"[ZIP] Waiting for networkidle...", file=sys.stderr)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
                print(f"[ZIP] Networkidle achieved", file=sys.stderr)
            except Exception as e:
                print(f"[ZIP] Networkidle timeout (continuing): {e}", file=sys.stderr)

            page.wait_for_timeout(2000)

            # Find all tab buttons (Cytoscape.js uses data-tab or similar)
            print(f"[ZIP] Looking for tabs...", file=sys.stderr)
            tabs = page.query_selector_all("button[data-tab], .tab-button, [role='tab']")
            print(f"[ZIP] Found {len(tabs)} tabs", file=sys.stderr)

            # If no tabs found, just export the whole page
            if not tabs:
                print(f"[ZIP] No tabs found, exporting full page", file=sys.stderr)
                temp_png = zip_path.parent / f"_temp_full.png"
                page.screenshot(path=str(temp_png), full_page=True)

                with zipfile.ZipFile(str(zip_path), 'w') as zf:
                    zf.write(str(temp_png), "flow_diagram.png")
                temp_png.unlink()
                browser.close()
                print(f"[ZIP] SUCCESS: Single image zipped", file=sys.stderr)
                return True

            # Export each tab
            print(f"[ZIP] Exporting {len(tabs)} tabs to ZIP", file=sys.stderr)
            with zipfile.ZipFile(str(zip_path), 'w') as zf:
                for i, tab in enumerate(tabs):
                    tab_name = tab.text_content().strip() or f"flow_{i}"
                    print(f"[ZIP] Tab {i}: clicking '{tab_name}'", file=sys.stderr)
                    tab.click()
                    page.wait_for_timeout(1000)

                    temp_png = zip_path.parent / f"_temp_tab_{i}.png"
                    print(f"[ZIP] Tab {i}: screenshotting...", file=sys.stderr)
                    page.screenshot(path=str(temp_png), full_page=True)
                    zf.write(str(temp_png), f"{tab_name}.png")
                    temp_png.unlink()
                    print(f"[ZIP] Tab {i}: added to ZIP", file=sys.stderr)

            browser.close()
            print(f"[ZIP] SUCCESS: ZIP created with {len(tabs)} images", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[ZIP] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return False


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
            ["🔑  Credentials",
             "🔎  Contact Search",
             "🔍  Contact Investigator",
             "↔️  Contact Diff",
             "⚡  Lambda Errors",
             "🔬  Flow Analyze",
             "🎬  Flow Replay",
             "📊  Log Insights"],
            key="nav",
            label_visibility="collapsed",
        )

    return active_name, active_meta, page.strip().lstrip("🔑🔎🔍↔️⚡🔬🎬📊 ")


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

            # ── Info + quick actions ──────────────────────────────────────────
            info_col, action_col = st.columns([3, 1])
            with info_col:
                # Show current key ID prefix so user can confirm they're updating the right creds
                creds    = read_aws_creds()
                cur_kid  = creds[profile_name].get("aws_access_key_id", "") if profile_name in creds else ""
                kid_hint = f"`{cur_kid[:8]}…`" if cur_kid else "*(not found in credentials file)*"
                st.markdown(
                    f"**Instance ID:** `{instance_id or '—'}`  \n"
                    f"**Region:** `{region or '—'}`  \n"
                    f"**Added:** {added_at[:10] if added_at else '—'}  \n"
                    f"**Current key ID:** {kid_hint}  \n"
                    f"**Status:** {vdetail or '—'}"
                )
            with action_col:
                if st.button("Check", key=f"chk_{profile_name}", use_container_width=True):
                    check_profile.clear()
                    with st.spinner("Checking…"):
                        result = check_profile(profile_name)
                    st.session_state[vkey] = result
                    st.rerun()
                if st.button("🗑 Delete", key=f"del_{profile_name}", use_container_width=True):
                    st.session_state[f"confirm_delete_{profile_name}"] = True

            if st.session_state.get(f"confirm_delete_{profile_name}"):
                st.warning(f"Delete **{display_name}**? This also removes it from `~/.aws/credentials`.")
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

            # ── Refresh credentials ───────────────────────────────────────────
            # Auto-open when credentials are known expired
            creds_expired = (valid is False)
            with st.expander(
                "🔄 Refresh credentials" + (" ← expired" if creds_expired else ""),
                expanded=creds_expired,
            ):
                st.caption(
                    "Paste individual values from the AWS access portal "
                    "(**Access keys → Option 3**), or fill only the fields that changed. "
                    "Leave a field blank to keep its current value."
                )
                with st.form(key=f"refresh_creds_{profile_name}"):
                    new_kid = st.text_input(
                        "AWS Access Key ID",
                        placeholder="ASIA…",
                        help="Starts with ASIA for temporary/SSO credentials",
                    )
                    new_secret = st.text_input(
                        "AWS Secret Access Key",
                        placeholder="Leave blank to keep current",
                        type="password",
                    )
                    new_token = st.text_area(
                        "AWS Session Token",
                        placeholder="IQoJb3Jp… (leave blank to keep current)",
                        height=100,
                    )
                    if st.form_submit_button("💾 Update credentials", type="primary"):
                        if not any([new_kid.strip(), new_secret.strip(), new_token.strip()]):
                            st.warning("Fill in at least one field to update.")
                        else:
                            existing = read_aws_creds()
                            sec = existing[profile_name] if profile_name in existing else {}
                            write_aws_creds(
                                profile_name,
                                new_kid.strip()    or sec.get("aws_access_key_id", ""),
                                new_secret.strip() or sec.get("aws_secret_access_key", ""),
                                new_token.strip()  or sec.get("aws_session_token", ""),
                            )
                            st.session_state.pop(vkey, None)
                            check_profile.clear()
                            st.success("Credentials updated.")
                            st.rerun()

            st.divider()

            # ── Settings edit form ────────────────────────────────────────────
            with st.form(key=f"edit_{profile_name}"):
                st.caption("Edit settings")
                ec1, ec2, ec3 = st.columns(3)
                with ec1:
                    new_dn  = st.text_input("Display name",  value=display_name)
                with ec2:
                    new_iid = st.text_input("Instance ID",   value=instance_id)
                with ec3:
                    new_reg = st.text_input("Region",        value=region)
                new_lg = st.text_input(
                    "Default log group",
                    value=meta.get("log_group", ""),
                    placeholder="/aws/connect/<alias>",
                    help="Pre-fills the log group field on Contact Investigator and Log Insights",
                )
                if st.form_submit_button("Save changes"):
                    save_profile(profile_name, new_dn, new_iid, new_reg, new_lg)
                    if new_lg.strip():
                        set_profile_log_group(profile_name, new_iid.strip(), new_lg.strip())
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
        "Queue":             names.get("queue") or (contact.get("QueueInfo") or {}).get("Name") or (contact.get("QueueInfo") or {}).get("Id"),
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
                    value=get_profile_log_group(active_name, instance_override or instance_id),
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
            set_profile_log_group(active_name, iid, log_group_resolved)
            st.toast("Log group saved as default for this profile.", icon="✅")

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
            "Queue":       qi.get("Name", qi.get("Id", "")),
        })

    df = pd.DataFrame(rows)
    st.caption(f"{len(contacts)} contact(s) — select 1 to investigate, 2 to compare")

    event = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        column_config={
            "Contact ID": st.column_config.TextColumn(width="medium"),
            "Queue":      st.column_config.TextColumn(width="medium"),
        },
    )

    selected = (event.selection.rows or []) if event and hasattr(event, "selection") else []
    if len(selected) == 1:
        cid = rows[selected[0]]["Contact ID"]
        st.info(f"Selected: `{cid}`")
        if st.button("🔍 Investigate →", type="primary"):
            st.session_state["prefill_contact_id"] = cid
            st.session_state["nav"] = "🔍  Contact Investigator"
            st.rerun()
    elif len(selected) == 2:
        cid_a = rows[selected[0]]["Contact ID"]
        cid_b = rows[selected[1]]["Contact ID"]
        st.info(f"Selected: `{cid_a[:8]}…`  and  `{cid_b[:8]}…`")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔍 Investigate A →"):
                st.session_state["prefill_contact_id"] = cid_a
                st.session_state["nav"] = "🔍  Contact Investigator"
                st.rerun()
        with c2:
            if st.button("↔️ Compare these contacts →", type="primary"):
                st.session_state["prefill_diff_a"] = cid_a
                st.session_state["prefill_diff_b"] = cid_b
                st.session_state["nav"] = "↔️  Contact Diff"
                st.rerun()
    elif len(selected) > 2:
        st.caption("Select 1 or 2 contacts.")


# ══════════════════════════════════════════════════════════════════════════════
# Page: Contact Diff
# ══════════════════════════════════════════════════════════════════════════════

def _diff_table(rows, show_all: bool = True):
    """Render a list of DiffRow as a colour-coded 4-column dataframe."""
    import pandas as pd
    if not rows:
        return
    display = rows if show_all else [r for r in rows if not r.match]
    if not display:
        st.caption("*(all values match)*")
        return
    data = [{
        "Field":     r.label,
        "Contact A": r.val_a,
        "Contact B": r.val_b,
        "":          "✓" if r.match else "✗",
    } for r in display]
    df = pd.DataFrame(data)

    def _style(row):
        base = "color: #9ca3af" if row[""] == "✓" else "font-weight:bold"
        return [base] * len(row)

    st.dataframe(
        df.style.apply(_style, axis=1),
        use_container_width=True,
        hide_index=True,
        column_config={"": st.column_config.TextColumn(width="small")},
    )


def page_contact_diff(active_name: str, active_meta: dict):
    st.header("↔️ Contact Diff")
    st.caption("Compare two contacts field-by-field: core metadata, attributes, and Contact Lens.")

    if not active_name:
        st.warning("Select or add a profile in the sidebar first.")
        return

    instance_id = active_meta.get("instance_id", "")
    region      = active_meta.get("region", "us-east-1")

    with st.form("diff_form"):
        c1, c2 = st.columns(2)
        with c1:
            cid_a = st.text_input("Contact A",
                                  value=st.session_state.pop("prefill_diff_a", ""),
                                  placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        with c2:
            cid_b = st.text_input("Contact B",
                                  value=st.session_state.pop("prefill_diff_b", ""),
                                  placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        show_all_attrs = st.checkbox("Show all attributes (not just differences)", value=False)
        submitted = st.form_submit_button("Compare", type="primary")

    if not submitted:
        return
    if not cid_a.strip() or not cid_b.strip():
        st.error("Both contact IDs are required.")
        return
    if cid_a.strip() == cid_b.strip():
        st.warning("Both IDs are the same — all fields will match.")

    try:
        sess   = boto3.Session(profile_name=active_name)
        client = sess.client("connect", region_name=region, config=RETRY_CONFIG)
    except Exception as e:
        st.error(str(e))
        return

    with st.spinner("Fetching contacts…"):
        try:
            contact_a = cd.fetch_contact(client, instance_id, cid_a.strip())
            contact_b = cd.fetch_contact(client, instance_id, cid_b.strip())
        except Exception as e:
            st.error(f"Could not load contact(s): {e}")
            return

        attrs_a = cd.fetch_attributes(client, instance_id, cid_a.strip())
        attrs_b = cd.fetch_attributes(client, instance_id, cid_b.strip())
        lens_a  = cd.collect_lens(client, instance_id, contact_a)
        lens_b  = cd.collect_lens(client, instance_id, contact_b)
        names_a = cd.resolve_names(client, instance_id, contact_a)
        names_b = cd.resolve_names(client, instance_id, contact_b)

    core_rows = cd.build_core_rows(contact_a, contact_b, names_a, names_b)
    attr_rows = cd.build_attr_rows(attrs_a, attrs_b)
    lens_rows = cd.build_lens_rows(lens_a, lens_b)

    n_diff = sum(1 for r in core_rows + attr_rows + lens_rows if not r.match)
    n_total = len(core_rows) + len(attr_rows) + len(lens_rows)
    st.caption(f"**{n_diff}** field(s) differ across {n_total} compared")

    st.subheader("Core")
    _diff_table(core_rows, show_all=True)

    st.subheader("Attributes")
    _diff_table(attr_rows, show_all=show_all_attrs)
    if not show_all_attrs and attr_rows:
        n_hidden = sum(1 for r in attr_rows if r.match)
        if n_hidden:
            st.caption(f"*{n_hidden} matching attribute(s) hidden — enable 'Show all' to see them*")

    st.subheader("Contact Lens")
    _diff_table(lens_rows, show_all=True)

    # Quick links
    st.divider()
    lc1, lc2 = st.columns(2)
    with lc1:
        if st.button(f"🔍 Investigate A ({cid_a[:8]}…)"):
            st.session_state["prefill_contact_id"] = cid_a.strip()
            st.session_state["nav"] = "🔍  Contact Investigator"
            st.rerun()
    with lc2:
        if st.button(f"🔍 Investigate B ({cid_b[:8]}…)"):
            st.session_state["prefill_contact_id"] = cid_b.strip()
            st.session_state["nav"] = "🔍  Contact Investigator"
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Page: Lambda Errors
# ══════════════════════════════════════════════════════════════════════════════

_LAMBDA_PERIODS = ["Last 24h", "Today", "Yesterday", "This week",
                   "Last week", "This month", "Last month", "Custom"]
_PERIOD_MAP = {
    "Today": "today", "Yesterday": "yesterday",
    "This week": "this-week", "Last week": "last-week",
    "This month": "this-month", "Last month": "last-month",
}


def _resolve_lambda_window(period_label: str, custom_n: int, custom_unit: str,
                           start_date, end_date):
    now = dt.datetime.now(dt.timezone.utc)
    if period_label in _PERIOD_MAP:
        return le._named_period(_PERIOD_MAP[period_label])
    if period_label == "Last 24h":
        return now - dt.timedelta(hours=24), now
    if period_label == "Custom":
        unit_map = {"hours": "h", "days": "d", "minutes": "m"}
        delta = le.parse_duration(f"{int(custom_n)}{unit_map[custom_unit]}")
        if start_date:
            s = dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
            e = dt.datetime.combine(end_date or dt.date.today(),
                                    dt.time(23, 59, 59), tzinfo=dt.timezone.utc)
            return s, e
        return now - delta, now
    return now - dt.timedelta(hours=24), now


def _render_error_group(error_type, errs, show_contact_ids, active_name, nav_key):
    count = len(errs)
    with st.expander(f"**{error_type}** — {count} occurrence(s)", expanded=(count >= 3)):
        rows = []
        for err in errs[:50]:
            row = {"Time": err["timestamp"].strftime("%Y-%m-%d %H:%M:%S")}
            if show_contact_ids:
                row["Contact ID"] = err.get("contact_id", "")
                row["Flow"]       = err.get("flow_name", "")
            else:
                row["Request ID"] = (err.get("request_id") or "")[:16]
                msg = err.get("message", "")
                row["Message"]    = msg[:80] + "…" if len(msg) > 80 else msg
            rows.append(row)

        import pandas as pd
        df = pd.DataFrame(rows)

        if show_contact_ids and "Contact ID" in df.columns:
            event = st.dataframe(df, use_container_width=True, hide_index=True,
                                 on_select="rerun", selection_mode="single-row")
            sel = (event.selection.rows or []) if event and hasattr(event, "selection") else []
            if sel:
                cid = rows[sel[0]].get("Contact ID", "")
                if cid and st.button("🔍 Investigate →", key=f"inv_{nav_key}_{cid[:8]}"):
                    st.session_state["prefill_contact_id"] = cid
                    st.session_state["nav"] = "🔍  Contact Investigator"
                    st.rerun()
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)

        if count > 50:
            st.caption(f"*Showing first 50 of {count} occurrences*")


def page_lambda_errors(active_name: str, active_meta: dict):
    st.header("⚡ Lambda Errors")
    st.caption("Aggregate Lambda errors from CloudWatch logs. "
               "Connect flow logs show which contacts were affected.")

    if not active_name:
        st.warning("Select or add a profile in the sidebar first.")
        return

    instance_id = active_meta.get("instance_id", "")
    region      = active_meta.get("region", "us-east-1")

    with st.form("lambda_err_form"):
        fn_input = st.text_input("Function name or ARN",
                                 placeholder="my-connect-lambda or arn:aws:lambda:…")
        period_label = st.selectbox("Time window", _LAMBDA_PERIODS, index=0)

        custom_n, custom_unit, start_d, end_d = 24, "hours", None, None
        if period_label == "Custom":
            cc1, cc2 = st.columns(2)
            with cc1:
                custom_n    = st.number_input("Amount", min_value=1, value=24)
                custom_unit = st.selectbox("Unit", ["hours", "days", "minutes"])
            with cc2:
                start_d = st.date_input("Or: start date", value=None)
                end_d   = st.date_input("Or: end date",   value=None)

        include_connect = st.checkbox(
            "Also search Connect flow logs (shows contact IDs for each error)",
            value=bool(instance_id),
            help="Requires instance ID on the active profile",
        )
        submitted = st.form_submit_button("Search", type="primary")

    if not submitted:
        return
    if not fn_input.strip():
        st.error("Function name is required.")
        return

    fn_name         = le.extract_function_name(fn_input.strip())
    lambda_lg       = f"/aws/lambda/{fn_name}"
    start_dt, end_dt = _resolve_lambda_window(period_label, custom_n, custom_unit, start_d, end_d)

    try:
        sess = boto3.Session(profile_name=active_name)
        logs_client    = sess.client("logs",    region_name=region, config=RETRY_CONFIG)
        connect_client = sess.client("connect", region_name=region, config=RETRY_CONFIG)
    except Exception as e:
        st.error(str(e))
        return

    start_ms = le._ms(start_dt)
    end_ms   = le._ms(end_dt)
    st.caption(f"Window: {start_dt.strftime('%Y-%m-%d %H:%M')} → "
               f"{end_dt.strftime('%Y-%m-%d %H:%M')} UTC")

    # ── Lambda log search ─────────────────────────────────────────────────────
    with st.spinner(f"Fetching Lambda logs from `{lambda_lg}`…"):
        try:
            lambda_events = le.filter_log_events(
                logs_client, lambda_lg, le._LAMBDA_ERROR_FILTER,
                start_ms, end_ms, missing_ok=True,
            )
        except SystemExit:
            st.error(f"Could not query `{lambda_lg}`. Check IAM and function name.")
            return
    lambda_errs = le.parse_lambda_log_errors(lambda_events)
    lambda_agg  = le.aggregate(lambda_errs)

    # ── Connect flow log search ───────────────────────────────────────────────
    connect_errs = []
    connect_agg  = None
    connect_lg   = None
    if include_connect and instance_id:
        connect_lg = get_profile_log_group(active_name, instance_id)
        if connect_lg:
            with st.spinner(f"Fetching Connect flow logs from `{connect_lg}`…"):
                try:
                    connect_events = le.filter_log_events(
                        logs_client, connect_lg, le._CONNECT_LAMBDA_FILTER,
                        start_ms, end_ms, missing_ok=True,
                    )
                except SystemExit:
                    connect_events = []
            connect_errs = le.parse_connect_flow_errors(connect_events, fn_input.strip())
            connect_agg  = le.aggregate(connect_errs)
        else:
            st.warning("No Connect log group set for this profile — "
                       "set it in Log Insights or Contact Investigator.")

    # ── Results ───────────────────────────────────────────────────────────────
    total = lambda_agg["total"] + (connect_agg["total"] if connect_agg else 0)
    if total == 0:
        st.success(f"No errors found for `{fn_name}` in this window.")
        return

    if lambda_agg["total"]:
        st.subheader(f"Lambda log errors — {lambda_agg['total']} total")
        for etype, errs in lambda_agg["by_type"].items():
            _render_error_group(etype, errs, show_contact_ids=False,
                                active_name=active_name, nav_key=f"ll_{etype}")

    if connect_agg and connect_agg["total"]:
        st.subheader(f"Connect flow log errors — {connect_agg['total']} total "
                     f"*(with contact IDs)*")
        for etype, errs in connect_agg["by_type"].items():
            _render_error_group(etype, errs, show_contact_ids=True,
                                active_name=active_name, nav_key=f"cl_{etype}")
    elif connect_agg:
        st.info("No Lambda invocation failures found in Connect flow logs for this function/window.")


# ══════════════════════════════════════════════════════════════════════════════
# Page: Flow Analyze
# ══════════════════════════════════════════════════════════════════════════════

def page_flow_analyze(active_name: str, active_meta: dict):
    st.header("🔬 Flow Analyze")
    st.caption("Scan for configuration errors and optimization suggestions.")

    if not active_name:
        st.warning("Select or add a profile in the sidebar first.")
        return

    instance_id = active_meta.get("instance_id", "")
    region      = active_meta.get("region", "us-east-1")

    # ── Flow source ───────────────────────────────────────────────────────────
    source = st.radio("Source", ["Instance flow", "Upload JSON file"],
                      horizontal=True, key="fa_source")

    content, flow_name, flow_type = None, "", ""

    if source == "Upload JSON file":
        uploaded = st.file_uploader("Flow JSON (from export_flow.py)", type=["json"])
        if uploaded:
            try:
                data = json.load(uploaded)
                if "content" in data and "Actions" in (data.get("content") or {}):
                    flow_name = (data.get("metadata") or {}).get("name") or uploaded.name
                    flow_type = (data.get("metadata") or {}).get("type") or ""
                    content   = data["content"]
                elif "Actions" in data:
                    flow_name, content = uploaded.name, data
                else:
                    st.error("File doesn't look like a contact flow (no 'Actions' array).")
            except Exception as e:
                st.error(f"Could not parse JSON: {e}")
    else:
        if not instance_id:
            st.warning("No instance ID on this profile.")
            return

        if st.button("Load flows", key="fa_load"):
            try:
                sess   = boto3.Session(profile_name=active_name)
                client = sess.client("connect", region_name=region, config=RETRY_CONFIG)
                flows  = fa.list_all_flows(client, instance_id)
                st.session_state["fa_flows"]  = sorted(flows, key=lambda f: f["Name"].lower())
                st.session_state["fa_profile"] = active_name
            except Exception as e:
                st.error(str(e))

        if active_name != st.session_state.get("fa_profile"):
            st.session_state.pop("fa_flows", None)

        flows = st.session_state.get("fa_flows", [])
        if not flows:
            st.info("Click **Load flows** to fetch the flow list from this instance.")
        else:
            flow_opts = {f["Name"]: f for f in flows}
            ftype_filter = st.selectbox(
                "Filter by type",
                ["All types"] + sorted({f.get("ContactFlowType", "") for f in flows}),
                key="fa_ftype",
            )
            if ftype_filter != "All types":
                flow_opts = {k: v for k, v in flow_opts.items()
                             if v.get("ContactFlowType") == ftype_filter}

            selected_name = st.selectbox("Flow", list(flow_opts.keys()), key="fa_flow_sel")
            flow_summary  = flow_opts.get(selected_name)

    # ── Mode + run ────────────────────────────────────────────────────────────
    mode = st.radio("Analysis", ["Scan + Optimize", "Scan only", "Optimize only"],
                    horizontal=True, key="fa_mode")

    do_scan     = mode in ("Scan + Optimize", "Scan only")
    do_optimize = mode in ("Scan + Optimize", "Optimize only")

    run_ready = (content is not None) or (source == "Instance flow" and
                st.session_state.get("fa_flows") and
                st.session_state.get("fa_flow_sel"))

    if not st.button("▶ Analyze", type="primary", disabled=not run_ready):
        return

    # ── Fetch content from instance if needed ─────────────────────────────────
    if source == "Instance flow" and content is None:
        flow_summary = st.session_state["fa_flows"][
            [f["Name"] for f in st.session_state["fa_flows"]].index(
                st.session_state["fa_flow_sel"]
            )
        ]
        with st.spinner(f"Loading '{flow_summary['Name']}'…"):
            try:
                sess   = boto3.Session(profile_name=active_name)
                client = sess.client("connect", region_name=region, config=RETRY_CONFIG)
                content = fa.describe_flow_content(client, instance_id, flow_summary["Id"])
            except Exception as e:
                st.error(str(e))
                return
        if content is None:
            st.error("Could not load flow content.")
            return
        flow_name = flow_summary["Name"]
        flow_type = flow_summary.get("ContactFlowType", "")

    n_blocks = len(content.get("Actions") or [])
    st.caption(f"**{flow_name}**  ·  {n_blocks} blocks  ·  {flow_type or 'unknown type'}")

    # ── Scan results ──────────────────────────────────────────────────────────
    if do_scan:
        issues   = fa.scan_flow(content)
        n_err    = sum(1 for i in issues if i.severity == "ERROR")
        n_wrn    = sum(1 for i in issues if i.severity == "WARN")
        if not issues:
            st.success("✓ Scan: no issues found")
        else:
            st.subheader(f"Scan — {len(issues)} issue(s): {n_err} ERROR · {n_wrn} WARN")
            by_block: dict = {}
            for iss in issues:
                by_block.setdefault((iss.block_id, iss.block_type), []).append(iss)
            for (bid, btype), block_issues in by_block.items():
                label = f"{bid[:24]}… ({btype})" if len(bid) > 24 else f"{bid} ({btype})"
                icons = "  ".join("🔴" if i.severity == "ERROR" else "🟡" for i in block_issues)
                with st.expander(f"{icons}  {label}"):
                    for iss in block_issues:
                        icon = "🔴" if iss.severity == "ERROR" else "🟡"
                        st.markdown(f"{icon} **{iss.kind.replace('_', ' ')}**")
                        st.caption(iss.detail)

    # ── Optimize results ──────────────────────────────────────────────────────
    if do_optimize:
        suggestions = fa.analyse_flow(content, flow_type)
        if not suggestions:
            st.success("✓ Optimize: no suggestions")
        else:
            cat_labels  = {"ux": "UX", "reliability": "Reliability",
                           "structure": "Structure", "maintainability": "Maintainability"}
            by_cat: dict = {}
            for s in suggestions:
                by_cat.setdefault(s.category, []).append(s)

            st.subheader(f"Optimize — {len(suggestions)} suggestion(s)")
            for cat, cat_label in cat_labels.items():
                items = by_cat.get(cat, [])
                if not items:
                    continue
                with st.expander(f"**{cat_label}** — {len(items)} suggestion(s)"):
                    for s in items:
                        icon = "🟡" if s.level == "WARN" else "💡"
                        loc  = f"`{s.block_id}`" if s.block_id else "*(flow level)*"
                        st.markdown(f"{icon} {loc}")
                        st.caption(s.detail)


# ══════════════════════════════════════════════════════════════════════════════
# Page: Flow Replay
# ══════════════════════════════════════════════════════════════════════════════

def page_flow_replay(active_name: str, active_meta: dict):
    st.header("🎬 Flow Replay")
    st.caption(
        "Visualize the exact path a real contact took through your flows. "
        "Pulls CloudWatch flow logs, reconstructs the scenario, and renders "
        "an interactive HTML flow graph."
    )

    if not active_name:
        st.warning("Select or add a profile in the sidebar first.")
        return

    instance_id = active_meta.get("instance_id", "")
    region      = active_meta.get("region", "us-east-1")

    if not instance_id:
        st.warning("No instance ID on this profile.")
        return

    # ── Flow cache check ──────────────────────────────────────────────────────
    flow_cache = Path.home() / ".connecttools" / "flows" / instance_id
    if not flow_cache.exists():
        st.warning(
            "**Flow map cache not found** for this instance.  \n"
            "Flow Replay requires a pre-built flow map. Build it once with:"
        )
        st.code(
            f"cd flowSim\n"
            f"python flow_map.py --instance-id {instance_id} --region {region}",
            language="bash",
        )
        if st.button("Build flow map now", type="primary"):
            flow_map_script = FLOWSIM_DIR / "flow_map.py"
            with st.spinner("Building flow map — fetching all flows from instance…"):
                cmd = [sys.executable, str(flow_map_script),
                       "--instance-id", instance_id, "--region", region,
                       "--profile", active_name]
                result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                st.success("Flow map built. You can now replay contacts.")
                st.rerun()
            else:
                st.error("flow_map.py failed:")
                st.code(result.stderr or result.stdout)
        return

    st.caption(f"Flow cache: `{flow_cache}`  ·  "
               f"{sum(1 for _ in flow_cache.glob('*.json'))} flow file(s) cached")

    # ── Save default log group ────────────────────────────────────────────────
    profile_lg = load_profiles().get(active_name, {}).get("log_group", "")
    instance_lg = ctcfg.get_log_group(instance_id)
    current_lg = profile_lg or instance_lg

    # Show which default is active
    if current_lg:
        source = "Profile" if profile_lg else "Instance"
        st.caption(f"Using {source} default: `{current_lg}`")
    else:
        st.caption("No default log group set (will auto-discover)")

    lg1, lg2 = st.columns([4, 1])
    with lg1:
        new_lg = st.text_input(
            "Default log group",
            value=current_lg,
            placeholder="/aws/connect/<alias>",
            key="flow_replay_lg_input",
        )
    with lg2:
        if st.button("💾 Save", help="Save as default for this profile"):
            if new_lg.strip():
                set_profile_log_group(active_name, instance_id, new_lg)
                st.success("Saved!")
                st.rerun()

    # Check if we have a saved replay result (for button clicks to work)
    saved_replay = st.session_state.get("_saved_replay", {})
    if saved_replay and saved_replay.get("cid"):
        # Resume from saved state
        cid = saved_replay["cid"]
        cid8 = cid[:8]
        log_group_val = saved_replay["log_group"]
        flow_override = ""
    else:
        # ── Form ──────────────────────────────────────────────────────────────────
        with st.form("replay_form"):
            contact_id = st.text_input(
                "Contact ID",
                value=st.session_state.pop("prefill_replay_cid", ""),
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            )
            log_group_val = st.text_input(
                "Log group (for this replay)",
                value=new_lg if "flow_replay_lg_input" in st.session_state else current_lg,
                placeholder="/aws/connect/<alias>",
            )
            flow_override = st.text_input(
                "Entry flow override",
                placeholder="Leave blank to auto-detect from logs",
                help="Only needed if the entry flow can't be detected from the logs",
            )
            submitted = st.form_submit_button("▶ Replay", type="primary")

        if not submitted:
            return
        if not contact_id.strip():
            st.error("Contact ID is required.")
            return

        cid = contact_id.strip()
        cid8 = cid[:8]

        # Save replay params for next rerun
        st.session_state["_saved_replay"] = {"cid": cid, "log_group": log_group_val}

    # ── Steps ─────────────────────────────────────────────────────────────────
    with st.status("Replaying contact…", expanded=True) as status:

        st.write("Fetching contact metadata…")
        try:
            sess    = boto3.Session(profile_name=active_name)
            connect = sess.client("connect", region_name=region, config=RETRY_CONFIG)
            cw      = sess.client("logs",    region_name=region, config=RETRY_CONFIG)
            meta    = rc._describe_contact(connect, instance_id, cid)
        except Exception as e:
            status.update(label="Failed", state="error")
            st.error(str(e))
            return

        initiated    = meta.get("InitiationTimestamp")
        disconnected = meta.get("DisconnectTimestamp")
        if not initiated:
            status.update(label="Failed", state="error")
            st.error("Contact has no InitiationTimestamp.")
            return

        now   = dt.datetime.now(dt.timezone.utc)
        start = initiated - dt.timedelta(minutes=1)
        end   = (disconnected + dt.timedelta(minutes=2)) if disconnected else now
        dur   = int((disconnected - initiated).total_seconds()) if disconnected else None
        st.write(f"Contact started {initiated.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                 + (f" · {dur}s" if dur else ""))

        lg = log_group_val.strip()
        if not lg:
            st.write("Resolving log group…")
            try:
                lg = rc._resolve_log_group(connect, instance_id)
            except Exception as e:
                status.update(label="Failed", state="error")
                st.error(str(e))
                return
        st.write(f"Log group: `{lg}`")

        st.write("Fetching flow logs…")
        try:
            raw_events = rc._fetch_events(cw, lg, cid, start, end)
        except SystemExit:
            status.update(label="Failed", state="error")
            st.error("Could not fetch logs. Check IAM and log group name.")
            return

        if not raw_events:
            status.update(label="No logs found", state="error")
            st.error(
                "No flow log events found.  \n"
                "Possible causes: logs outside CloudWatch retention (30 days), "
                "contact never entered a flow, or wrong log group."
            )
            return
        st.write(f"{len(raw_events)} log event(s) found.")

        st.write("Reconstructing contact path…")
        contact_rec  = rc._reconstruct(raw_events, cid)
        if not contact_rec:
            status.update(label="Failed", state="error")
            st.error("Could not reconstruct contact record from log events.")
            return

        initial_flow = flow_override.strip() or contact_rec.get("initial_flow", "")
        if not initial_flow:
            status.update(label="Failed", state="error")
            st.error(
                "Could not detect the entry flow from logs. "
                "Fill in the **Entry flow override** field and try again."
            )
            return

        n_lambdas = len(contact_rec.get("lambda_calls") or [])
        n_dtmf    = len(contact_rec.get("dtmf") or [])
        st.write(f"Entry flow: **{initial_flow}**  ·  "
                 f"{n_lambdas} Lambda call(s)  ·  {n_dtmf} DTMF input(s)")

        st.write("Building scenario…")
        scenario      = rc._build_scenario(contact_rec)
        scenarios_dir = FLOWSIM_DIR / "Scenarios"
        sims_dir      = FLOWSIM_DIR / "Simulations"
        scenarios_dir.mkdir(parents=True, exist_ok=True)
        sims_dir.mkdir(parents=True, exist_ok=True)
        scenario_path = scenarios_dir / f"replay_{cid8}.json"
        html_path     = sims_dir      / f"replay_{cid8}.html"
        scenario_path.write_text(json.dumps(scenario, indent=2), encoding="utf-8")

        st.write("Running simulation…")
        cmd = [
            sys.executable, str(FLOWSIM_DIR / "flow_sim.py"),
            "--instance-id", instance_id,
            "--flow",        initial_flow,
            "--scenario",    str(scenario_path),
            "--html",        str(html_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            status.update(label="Simulation failed", state="error")
            st.error("flow_sim.py failed:")
            st.code(result.stderr or result.stdout)
            return

        status.update(label="Replay complete!", state="complete")

    # ── Render HTML ───────────────────────────────────────────────────────────
    html_path = FLOWSIM_DIR / "Simulations" / f"replay_{cid8}.html"
    if not html_path.exists():
        st.error("HTML file was not produced.")
        return

    html_content = html_path.read_text(encoding="utf-8")
    st.success(f"Replayed `{cid8}…`  ·  {len(raw_events)} log events  ·  "
               f"{n_lambdas} Lambda call(s)")

    import streamlit.components.v1 as components

    # Full-screen toggle
    if "_flow_replay_fullscreen" not in st.session_state:
        st.session_state._flow_replay_fullscreen = False

    if st.button("🖥️ " + ("Exit Full Screen" if st.session_state._flow_replay_fullscreen else "Full Screen")):
        st.session_state._flow_replay_fullscreen = not st.session_state._flow_replay_fullscreen
        st.rerun()

    # Display diagram (full-screen or normal)
    if st.session_state._flow_replay_fullscreen:
        components.html(html_content, height=900, scrolling=True)
    else:
        components.html(html_content, height=720, scrolling=False)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button(
            "⬇ Download HTML",
            data=html_content,
            file_name=f"replay_{cid8}.html",
            mime="text/html",
        )
    with col2:
        sims_dir = FLOWSIM_DIR / "Simulations"
        png_path = sims_dir / f"replay_{cid8}.png"

        if png_path.exists():
            png_data = png_path.read_bytes()
            st.download_button(
                "⬇ Download PNG",
                data=png_data,
                file_name=f"replay_{cid8}.png",
                mime="image/png",
                key=f"download_png_{cid8}",
            )
        else:
            if st.button("📸 Generate PNG"):
                with st.spinner("Rendering PNG..."):
                    if html_to_png(html_content, png_path):
                        st.rerun()
                    else:
                        st.error("Failed to generate PNG. Ensure Playwright is installed: `pip install playwright && playwright install`")

    with col3:
        zip_path = sims_dir / f"replay_{cid8}_all_flows.zip"

        if zip_path.exists():
            zip_data = zip_path.read_bytes()
            st.download_button(
                "⬇ Download ZIP",
                data=zip_data,
                file_name=f"replay_{cid8}_flows.zip",
                mime="application/zip",
                key=f"download_zip_{cid8}",
            )
        else:
            if st.button("📦 Export All Tabs"):
                with st.spinner("Rendering all tabs..."):
                    if html_export_all_tabs(html_content, zip_path):
                        st.rerun()
                    else:
                        st.error("Failed to export tabs. Ensure Playwright is installed: `pip install playwright && playwright install`")

    # Quick links
    lc1, lc2 = st.columns(2)
    with lc1:
        if st.button("🔍 Investigate this contact →"):
            st.session_state["prefill_contact_id"] = cid
            st.session_state["nav"] = "🔍  Contact Investigator"
            st.rerun()
    with lc2:
        st.caption(f"Scenario saved → `{scenario_path.name}`")


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

    # Reset page-specific state when the active profile changes
    if active_name != st.session_state.get("li_active_profile"):
        st.session_state["li_active_profile"] = active_name
        st.session_state.pop("li_log_group_val", None)
        st.session_state["li_lg_key"] = st.session_state.get("li_lg_key", 0) + 1
        st.session_state.pop("li_discovered", None)

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

    saved_lg = get_profile_log_group(active_name, active_meta.get("instance_id", ""))

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
            set_profile_log_group(active_name, iid, log_group.strip())
            st.toast("Log group saved as default for this profile.", icon="✅")

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
    elif "Contact Diff" in page:
        page_contact_diff(active_name, active_meta)
    elif "Lambda Errors" in page:
        page_lambda_errors(active_name, active_meta)
    elif "Flow Analyze" in page:
        page_flow_analyze(active_name, active_meta)
    elif "Flow Replay" in page:
        page_flow_replay(active_name, active_meta)
    elif "Log Insights" in page:
        page_log_insights(active_name, active_meta)


if __name__ == "__main__":
    main()
