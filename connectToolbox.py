#!/usr/bin/env python3
"""connectToolbox.py — Interactive launcher for Amazon Connect Tools."""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

import ct_config

# Force UTF-8 output immediately so box-drawing chars work on Windows.
# line_buffering=True ensures every print() flushes, which is required when
# stdout is redirected/piped (e.g. mintty) and Python can't detect a tty.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

SCRIPT_DIR  = Path(__file__).parent
QUERIES_DIR = SCRIPT_DIR / "queries"
TITLE       = "Amazon Connect Tools"
LOG_FILE    = Path.home() / "logs" / "connecttools.log"

_cfg = ct_config.load()


_PLACEHOLDER_RE = re.compile(r"{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}")


class GoBack(Exception):
    """Raised from any prompt when the user types '..' to return to the main menu."""


# ── Raw keypress reader (cross-platform) ──────────────────────────────────────
# mintty (Git Bash on Windows) sets TERM but msvcrt doesn't work there.
# Detect it and use a simple line-input fallback instead.

_MINTTY = sys.platform == "win32" and bool(os.environ.get("TERM"))

if _MINTTY:
    # mintty never calls getch() — pick_menu uses plain input() instead
    def getch() -> bytes:
        return b""

    UP    = b""
    DOWN  = b""
    CLEAR = "clear"
elif sys.platform == "win32":
    import msvcrt

    def getch() -> bytes:
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            ch = b"\xe0" + msvcrt.getch()
        return ch

    UP    = b"\xe0H"
    DOWN  = b"\xe0P"
    CLEAR = "cls"
else:
    import os as _os
    import select
    import termios
    import tty

    def getch() -> bytes:
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = _os.read(fd, 1)
            if ch == b"\x1b":
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if r:
                    ch += _os.read(fd, 2)
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    UP    = b"\x1b[A"
    DOWN  = b"\x1b[B"
    CLEAR = "clear"

QUIT = (b"q", b"Q", b"\x03")


def clear_screen():
    if _MINTTY:
        print("\n" + "─" * 60, flush=True)  # scroll separator — ANSI clear unreliable in mintty
    else:
        os.system(CLEAR)


# ── Generic arrow-key menu ────────────────────────────────────────────────────

def pick_menu(
    title: str,
    options: list,
    quit_label: str = "back",
    descriptions: list = None,
) -> int | None:
    """Arrow-key or number selection. Returns 0-based index or None to go back/quit.

    options: list of display names (str)
    descriptions: optional list of short tooltip strings, same length as options
    """
    n    = len(options)
    desc = descriptions or [""] * n

    if _MINTTY:
        while True:
            clear_screen()
            print(f"\n  {title}")
            print("  " + "─" * max(40, len(title) + 2))
            print()
            for i, name in enumerate(options):
                num = str(i + 1) if i < 9 else " "
                print(f"     {num}.  {name}")
                if desc[i]:
                    print(f"          \033[90m{desc[i]}\033[0m")
            print()
            print(f"  \033[90m[1-{min(n, 9)}] select  [q] {quit_label}\033[0m\n")
            val = _input("  Choice (press Enter to confirm): ").strip()
            if not val or val.lower().startswith("q"):
                return None
            if val.isdigit() and 1 <= int(val) <= n:
                return int(val) - 1
            print(f"  \033[33m  Enter a number 1–{min(n, 9)}\033[0m")

    selected = 0
    while True:
        clear_screen()
        print(f"\n  {title}")
        print("  " + "─" * max(40, len(title) + 2))
        print()
        for i, name in enumerate(options):
            num = str(i + 1) if i < 9 else " "
            if i == selected:
                print(f"  \033[7m  {num}.  {name:<30}\033[0m")
                if desc[i]:
                    print(f"       \033[90m{desc[i]}\033[0m")
            else:
                print(f"     {num}.  {name}")
                if desc[i]:
                    print(f"          \033[90m{desc[i]}\033[0m")
        print()
        print(f"  \033[90m[↑↓] navigate  [Enter/1-{min(n, 9)}] select  [q] {quit_label}\033[0m")

        key = getch()
        if key in QUIT:
            return None
        if key == UP:
            selected = (selected - 1) % n
        elif key == DOWN:
            selected = (selected + 1) % n
        elif key in (b"\r", b"\n"):
            return selected
        elif len(key) == 1 and b"1" <= key <= b"9":
            i = int(key.decode()) - 1
            if i < n:
                return i


# ── Input helper ──────────────────────────────────────────────────────────────
# On Windows, input() uses the Windows console API for both writing the prompt
# and reading the response. In mintty (Git Bash) that API is invisible — mintty
# communicates via pipes, not the Windows console buffer — so prompts never
# appear and responses may be lost. Using sys.stdout.write + sys.stdin.readline
# goes through regular file I/O, which mintty's pipe handles correctly.

def _input(prompt: str = "") -> str:
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:          # EOF (Ctrl+D / Ctrl+Z)
        raise EOFError
    return line.rstrip("\r\n")


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _header(*crumbs: str):
    clear_screen()
    print(f"\n  {TITLE}  ›  {'  ›  '.join(crumbs)}")
    print("  " + "─" * 40)
    print(f"  \033[90m  type .. at any prompt to go back\033[0m")
    print()


def ask(label: str, required: bool = True, default: str = "") -> str:
    hint = f"[{default}]" if default else ("required" if required else "optional")
    while True:
        val = _input(f"  {label} ({hint}): ").strip()
        if val == "..":
            raise GoBack
        if not val and default:
            return default
        if not val and required:
            print("  \033[33m  ↑ this field is required\033[0m")
            continue
        return val

def ask_choice(label: str, choices: list[str], default: str) -> str:
    opts = "  ".join(f"{i + 1}) {c}" for i, c in enumerate(choices))
    print(f"  {label}:  {opts}")
    while True:
        val = _input(f"  Choice [{default}]: ").strip()
        if val == "..":
            raise GoBack
        if not val:
            return default
        if val.isdigit() and 1 <= int(val) <= len(choices):
            return choices[int(val) - 1]
        if val in choices:
            return val
        print(f"  \033[33m  Enter a number 1–{len(choices)}\033[0m")


# ── Date / time input helpers ─────────────────────────────────────────────────

import re as _re

# Accepted patterns: (regex, strptime_format)
_DATE_FORMATS = {
    "YYYY-MM":             (_re.compile(r"^\d{4}-\d{2}$"),                    "%Y-%m"),
    "YYYY-MM-DD":          (_re.compile(r"^\d{4}-\d{2}-\d{2}$"),              "%Y-%m-%d"),
    "YYYY-MM-DDTHH:MM:SS": (_re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"), "%Y-%m-%dT%H:%M:%S"),
    "YYYY-MM-DD HH:MM":    (_re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"), "%Y-%m-%d %H:%M"),
}


def _normalize_date(s: str) -> str:
    """Insert missing dashes and colons for common compact date/time strings."""
    s = s.strip()
    # 6 digits → YYYY-MM
    if _re.fullmatch(r"\d{6}", s):
        return f"{s[:4]}-{s[4:]}"
    # 8 digits → YYYY-MM-DD
    if _re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    # 8 digits + T/space + 4 digits → YYYY-MM-DD[T ]HH:MM
    if _re.fullmatch(r"\d{8}[T ]\d{4}", s):
        sep = s[8]
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}{sep}{s[9:11]}:{s[11:]}"
    # 8 digits + T/space + 6 digits → YYYY-MM-DD[T ]HH:MM:SS
    if _re.fullmatch(r"\d{8}[T ]\d{6}", s):
        sep = s[8]
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}{sep}{s[9:11]}:{s[11:13]}:{s[13:]}"
    # YYYY-MM-DD + T/space + 4 digits (missing colons) → YYYY-MM-DD[T ]HH:MM
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{4}", s):
        sep = s[10]
        return f"{s[:10]}{sep}{s[11:13]}:{s[13:]}"
    # YYYY-MM-DD + T/space + 6 digits (missing colons) → YYYY-MM-DD[T ]HH:MM:SS
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{6}", s):
        sep = s[10]
        return f"{s[:10]}{sep}{s[11:13]}:{s[13:15]}:{s[15:]}"
    return s


def _parse_date(s: str, strptime_fmt: str) -> bool:
    """Return True if s parses cleanly as strptime_fmt."""
    try:
        dt.datetime.strptime(s, strptime_fmt)
        return True
    except ValueError:
        return False

def ask_date(label: str, formats: list, required: bool = True, default: str = "") -> str:
    """Prompt for a date/time string, auto-normalize, validate, and re-prompt on bad input.

    formats: list of keys from _DATE_FORMATS, e.g. ["YYYY-MM-DD", "YYYY-MM-DDTHH:MM:SS"]
    Checks both shape (regex) and value validity (strptime) so e.g. month 13 is rejected.
    """
    hint_str = " or ".join(formats)
    fmt_specs = [_DATE_FORMATS[f] for f in formats]
    full_label = f"{label} ({hint_str})"
    while True:
        raw = ask(full_label, required=required, default=default)
        if not raw:
            return raw  # optional and left blank
        normalized = _normalize_date(raw)
        if normalized != raw:
            print(f"  \033[90m  → {normalized}\033[0m")
        matched = next(
            (strptime_fmt for pattern, strptime_fmt in fmt_specs if pattern.match(normalized)),
            None,
        )
        if matched and _parse_date(normalized, matched):
            return normalized
        if matched:
            print(f"  \033[33m  Invalid date value — check month/day are in range\033[0m")
        else:
            print(f"  \033[33m  Expected: {hint_str}\033[0m")

def ask_bool(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    val  = _input(f"  {label} [{hint}]: ").strip().lower()
    if val == "..":
        raise GoBack
    return default if not val else val in ("y", "yes")


# ── Config helpers ────────────────────────────────────────────────────────────

def _offer_save(iid: str, region: str, profile: str) -> None:
    """Offer to save instance_id/region/profile as defaults if they differ from config."""
    new = {"instance_id": iid, "region": region, "profile": profile}
    current = {k: _cfg.get(k, "") for k in new}
    if new == current:
        return  # nothing changed
    if any(current.values()):
        if not ask_bool("Overwrite saved defaults?", default=False):
            return
    else:
        if not ask_bool("Save as defaults?", default=True):
            return
    _cfg.update(new)
    ct_config.save(_cfg)
    print(f"  \033[90mSaved to {ct_config.CONFIG_FILE}\033[0m")

def ask_connect_defaults() -> tuple:
    """Prompt for instance ID, region, and profile with config-backed defaults.

    Returns (iid, region, profile). Offers to save if values differ from config.
    """
    iid     = ask("Instance ID", default=_cfg.get("instance_id", ""))
    region  = ask("Region",      required=False, default=_cfg.get("region", ""))
    profile = ask("Profile",     required=False, default=_cfg.get("profile", ""))
    _offer_save(iid, region, profile)
    return iid, region, profile

def connect_args(iid: str, region: str, profile: str) -> list:
    """Build the common --instance-id / --region / --profile arg list."""
    args = ["--instance-id", iid]
    if region:  args += ["--region",  region]
    if profile: args += ["--profile", profile]
    return args


# ── Tool runner + logging ────────────────────────────────────────────────────

def _log(script: str, args: list[str], returncode: int, elapsed: float):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tool   = script.replace(".py", "")
        status = f"exit={returncode}"
        if returncode != 0:
            status += "  ERROR"
        line = (
            f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            f"  {tool:<22}  {status:<16}  {elapsed:.1f}s"
            f"  {' '.join(args)}\n"
        )
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # never let logging break the launcher

def _run(script: str, args: list[str]):
    print()
    print("  " + "─" * 40)
    print()
    start  = dt.datetime.now()
    result = subprocess.run([sys.executable, str(SCRIPT_DIR / script)] + args)
    elapsed = (dt.datetime.now() - start).total_seconds()
    _log(script, args, result.returncode, elapsed)
    print()
    print("  " + "─" * 40)
    _input("  Press Enter to return to menu…")


# ── Tool definitions ────────────────────────────────────────────────────

CONTACTS_HANDLED_QUESTIONS = [
    {"label": "Month", "arg": "--month", "type": "date", "formats": ["YYYY-MM"], "required": False},
    {"label": "Timezone", "arg": "--timezone", "required": False},
]

CONTACT_INSPECT_QUESTIONS = [
    {"label": "Contact ID", "arg": "--contact-id", "required": True},
    {"label": "Include full transcript?", "arg": "--transcript", "type": "bool"},
]

CONTACT_TIMELINE_QUESTIONS = [
    {"label": "Contact ID", "arg": "--contact-id", "required": True},
    {"label": "Include transcript turns?", "arg": "--transcript", "type": "bool"},
    {"label": "Log group (leave blank to auto-discover)", "arg": "--log-group", "required": False},
]

CONTACT_DIFF_QUESTIONS = [
    {"label": "Contact ID A", "arg": "--contact-id-a", "required": True},
    {"label": "Contact ID B", "arg": "--contact-id-b", "required": True},
    {"label": "Show all attributes (not just differing)?", "arg": "--all-attrs", "type": "bool"},
]

CONTACT_RECORDINGS_QUESTIONS = [
    {"label": "Contact ID", "arg": "--contact-id", "required": True},
    {"label": "URL expiry (secs)", "arg": "--url-expires", "required": False, "default": "3600"},
]

FLOW_TO_CHART_QUESTIONS = [
    {"label": "Flow JSON file path", "arg": None, "standalone": True, "required": True},
    {"label": "Format", "arg": "--format", "type": "choice", "choices": ["html", "mermaid", "dot"], "default": "html"},
    {"label": "Output file", "arg": "--output", "required": False},
]

CID_JOURNEY_QUESTIONS = [
    {"label": "xlsx file path (from CID_Search Log Insights run)", "arg": None, "standalone": True, "required": True},
    {"label": "Output HTML file", "arg": "--output", "required": False},
]

AGENT_LIST_QUESTIONS = [
    {"label": "Search username (leave blank for all)", "arg": "--search", "required": False},
    {"label": "Filter by routing profile name", "arg": "--routing-profile", "required": False},
    {"label": "Output CSV file (leave blank to print table)", "arg": "--csv", "required": False},
]

def tool_runner(tool_name: str, script_name: str, questions: list, connect_tool: bool = True):
    _header(tool_name)
    args = []
    if connect_tool:
        iid, region, profile = ask_connect_defaults()
        args = connect_args(iid, region, profile)

    for q in questions:
        q_type = q.get("type", "text")
        label = q["label"]
        arg_name = q.get("arg")
        required = q.get("required", False)
        default = q.get("default", "")
        
        val = None

        if q_type == "date":
            formats = q["formats"]
            val = ask_date(label, formats, required=required, default=default)
        elif q_type == "choice":
            choices = q["choices"]
            val = ask_choice(label, choices, default=default)
        elif q_type == "bool":
            val = ask_bool(label, default=default)
        else: # text
            val = ask(label, required=required, default=default)

        if val:
            # Check if the question is a standalone argument
            if "standalone" in q and q["standalone"]:
                args.append(str(val))
            elif arg_name:
                if isinstance(val, bool):
                    args.append(arg_name)
                else:
                    # For some cases, the value of the argument is not what we want to pass to the script
                    # For example, in tool_contact_logs, the format can be "json" or "text"
                    # but we only want to pass "--text" if the format is "text"
                    if "val_map" in q and str(val) in q["val_map"]:
                        mapped_val = q["val_map"][str(val)]
                        if mapped_val: # mapped_val can be None if we don't want to add any argument
                            args.append(mapped_val)
                    else:
                        args += [arg_name, str(val)]

    _run(script_name, args)


# ── Tool: Contacts Handled ────────────────────────────────────────────────────


def tool_contacts_handled():
    tool_runner("Contacts Handled", "contacts_handled.py", CONTACTS_HANDLED_QUESTIONS)


# ── Tool: Contact Inspect ─────────────────────────────────────────────────────


def tool_contact_inspect():
    tool_runner("Contact Inspect", "contact_inspect.py", CONTACT_INSPECT_QUESTIONS)


# ── Tool: Contact Timeline ────────────────────────────────────────────────────


def tool_contact_timeline():
    tool_runner("Contact Timeline", "contact_timeline.py", CONTACT_TIMELINE_QUESTIONS)


# ── Tool: Contact Diff ────────────────────────────────────────────────────────


def tool_contact_diff():
    tool_runner("Contact Diff", "contact_diff.py", CONTACT_DIFF_QUESTIONS)


# ── Tool: Contact Search ──────────────────────────────────────────────────────

def tool_contact_search():
    _header("Contact Search")
    iid, region, profile = ask_connect_defaults()
    start = ask_date("Start", ["YYYY-MM-DD", "YYYY-MM-DDTHH:MM:SS"])
    end   = ask_date("End",   ["YYYY-MM-DD", "YYYY-MM-DDTHH:MM:SS"])

    args = connect_args(iid, region, profile) + ["--start", start, "--end", end]

    if ask_bool("Filter by channel?"):
        ch = ask_choice("Channel", VALID_CHANNELS_CS, default="VOICE")
        args += ["--channel", ch]

    if ask_bool("Filter by initiation method?"):
        method = ask_choice(
            "Initiation method",
            ["INBOUND", "OUTBOUND", "TRANSFER", "CALLBACK", "API",
             "QUEUE_TRANSFER", "EXTERNAL_OUTBOUND", "MONITOR", "DISCONNECT"],
            default="INBOUND",
        )
        args += ["--initiation-method", method]

    if ask_bool("Filter by agent login?"):
        login = ask("Agent login")
        args += ["--agent-login", login]

    if ask_bool("Filter by queue ID?"):
        qid = ask("Queue ID")
        args += ["--queue", qid]

    if ask_bool("Filter by contact attribute?"):
        kv = ask("Attribute (KEY=VALUE)")
        args += ["--attribute", kv]

    limit = ask("Max contacts to return", required=False)
    if limit:
        args += ["--limit", limit]

    output = ask("Output CSV file", required=False)
    if output:
        args += ["--output", output]

    _run("contact_search.py", args)


VALID_CHANNELS_CS = ["VOICE", "CHAT", "TASK", "EMAIL"]


# ── Tool: Contact Logs ────────────────────────────────────────────────────────

def tool_contact_logs():
    _header("Contact Logs")
    iid, region, profile = ask_connect_defaults()
    cid       = ask("Contact ID")
    log_group = ask("Log group", required=False, default=ct_config.get_log_group(iid))
    fmt       = ask_choice("Output format", ["json", "text"], default="json")
    output    = ask("Output file", required=False)

    if log_group and log_group != ct_config.get_log_group(iid):
        if ask_bool("Save log group for this instance?", default=True):
            ct_config.set_log_group(_cfg, iid, log_group)

    args = connect_args(iid, region, profile) + ["--contact-id", cid]
    if log_group: args += ["--log-group", log_group]
    if fmt == "text": args += ["--text"]
    if output:        args += ["--output", output]

    _run("contact_logs.py", args)


# ── Tool: Lambda Tracer ───────────────────────────────────────────────────────

def tool_lambda_tracer():
    _header("Lambda Tracer")
    iid, region, profile = ask_connect_defaults()
    cid       = ask("Contact ID")
    log_group = ask("Log group", required=False, default=ct_config.get_log_group(iid))
    output    = ask("Output file (leave blank to print)", required=False)

    if log_group and log_group != ct_config.get_log_group(iid):
        if ask_bool("Save log group for this instance?", default=True):
            ct_config.set_log_group(_cfg, iid, log_group)

    summary = not ask_bool("Show full Lambda log lines?", default=True)

    args = connect_args(iid, region, profile) + ["--contact-id", cid]
    if log_group: args += ["--log-group", log_group]
    if output:    args += ["--output",    output]
    if summary:   args += ["--summary"]

    _run("lambda_tracer.py", args)


# ── Tool: Contact Recordings ─────────────────────────────────────────────────


def tool_contact_recordings():
    tool_runner("Contact Recordings", "contact_recordings.py", CONTACT_RECORDINGS_QUESTIONS)


# ── Tool: Export Flow ─────────────────────────────────────────────────────────

def tool_export_flow():
    _header("Export Flow")
    iid, region, profile = ask_connect_defaults()

    if ask_bool("List available flows first?"):
        print()
        subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "export_flow.py")]
            + connect_args(iid, region, profile) + ["--list"]
        )
        print()

    name   = ask("Flow name")
    exact  = ask_bool("Exact name match?")
    output = ask("Output file", required=False)

    args = connect_args(iid, region, profile) + ["--name", name]
    if exact:  args += ["--exact"]
    if output: args += ["--output", output]

    _run("export_flow.py", args)


# ── Tool: Flow to Chart ───────────────────────────────────────────────────────


def tool_flow_to_chart():
    tool_runner("Flow to Chart", "flow_to_chart.py", FLOW_TO_CHART_QUESTIONS, connect_tool=False)


# ── Tool: Log Insights ────────────────────────────────────────────────────────

def _list_queries() -> list[Path]:
    if not QUERIES_DIR.exists():
        return []
    return sorted(p for p in QUERIES_DIR.iterdir() if p.suffix in (".sql", ".txt"))

def _display_name(path: Path) -> str:
    """CID_Search.sql → CID Search"""
    return path.stem.replace("_", " ")

def _parse_display_columns(query: str) -> list[str] | None:
    """Extract column names from the | display line, or None if not present."""
    m = re.search(r"^\s*\|\s*display\s+(.+)$", query, re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    return [c.strip() for c in m.group(1).split(",") if c.strip()]

def _select_columns(columns: list[str]) -> list[str]:
    """Let the user exclude columns by number. Returns the kept columns."""
    print("  Columns to include (type a number to exclude, blank when done):\n")
    excluded: set[int] = set()
    for i, col in enumerate(columns, 1):
        print(f"    {i:2}.  {col}")
    print()
    while True:
        val = _input("  Exclude # (or blank to finish): ").strip()
        if not val:
            break
        if val.isdigit() and 1 <= int(val) <= len(columns):
            idx = int(val) - 1
            excluded.add(idx)
            print(f"  \033[90m  ✕ {columns[idx]}\033[0m")
        else:
            print(f"  \033[33m  Enter a number 1–{len(columns)}\033[0m")
    return [c for i, c in enumerate(columns) if i not in excluded]

def _rewrite_display(query: str, columns: list[str]) -> str:
    """Replace the | display line with the given columns."""
    new_display = "| display " + ", ".join(columns)
    return re.sub(
        r"^\s*|\s*display\s+.+$", new_display,
        query,
        flags=re.IGNORECASE | re.MULTILINE,
    )

def _read_adhoc_query() -> str | None:
    """Prompt the user to paste a multi-line query. Returns text or None to cancel."""
    _header("Log Insights", "Ad Hoc Query")
    print("  Paste your query below, then press Ctrl+D (Linux/CloudShell) or Ctrl+Z Enter (Windows).")
    print("  \033[90m  (type 'cancel' on its own line to go back)\033[0m\n")
    lines: list[str] = []
    try:
        while True:
            line = _input()
            if line.strip().lower() == "cancel":
                return None
            lines.append(line)
    except EOFError:
        pass
    return "\n".join(lines).strip() or None

def tool_log_insights():
    # ── Pick a query ──────────────────────────────────────────────────────────
    queries = _list_queries()
    AD_HOC  = "Ad Hoc Query"

    names = [AD_HOC] + [_display_name(q) for q in queries]
    idx   = pick_menu(f"{TITLE}  ›  Log Insights", names, quit_label="back")
    if idx is None:
        return

    import tempfile

    if idx == 0:
        # Ad hoc path — paste query directly
        query_text = _read_adhoc_query()
        if not query_text:
            return
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".sql", delete=False, encoding="utf-8"
        )
        tmp.write(query_text)
        tmp.close()
        query_path = Path(tmp.name)
        query_name = "Ad Hoc"
    else:
        query_path = queries[idx - 1]
        query_text = query_path.read_text(encoding="utf-8").strip()
        query_name = names[idx]

    # ── Prompt for placeholders ───────────────────────────────────────────────
    placeholders = list(dict.fromkeys(_PLACEHOLDER_RE.findall(query_text)))

    _header("Log Insights", query_name)

    var_args: list[str] = []
    ph_values: dict[str, str] = {}
    for ph in placeholders:
        val = ask(ph.replace("_", " "))
        ph_values[ph] = val
        var_args += ["--var", f"{ph}={val}"]

    # ── Column selection ──────────────────────────────────────────────────────
    columns = _parse_display_columns(query_text)

    if columns and ask_bool("Customize columns?"):
        print()
        kept = _select_columns(columns)
        if kept and kept != columns:
            # Write a temp file with the modified display line;
            # placeholders are still present — log_insights.py will resolve via --var
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".sql", delete=False, encoding="utf-8"
            )
            tmp.write(_rewrite_display(query_text, kept))
            tmp.close()
            query_path = Path(tmp.name)
        print()

    # ── Time range ────────────────────────────────────────────────────────────
    time_choice = ask_choice("Time range", ["Relative (e.g. 24h, 7d)", "Date range"], default="Relative (e.g. 24h, 7d)")
    time_args: list[str] = []
    if "Date" in time_choice:
        start = ask_date("Start", ["YYYY-MM-DD", "YYYY-MM-DD HH:MM"])
        end   = ask_date("End",   ["YYYY-MM-DD", "YYYY-MM-DD HH:MM"], required=False)
        time_args = ["--start", start]
        if end:
            time_args += ["--end", end]
    else:
        last = ask("Duration", default="24h")
        time_args = ["--last", last]

    # ── Other options ─────────────────────────────────────────────────────────
    region    = ask("Region",     required=False, default=_cfg.get("region", ""))
    log_group = ask("Log group",  required=False)
    limit     = ask("Max rows",   required=False, default="1000")

    # Auto-name output for CID searches: CID-<value>_YYYY-MM-DD.xlsx
    if "CID" in ph_values:
        import datetime as _dt
        today  = _dt.date.today().strftime("%Y-%m-%d")
        output = f"CID-{ph_values['CID']}_{today}.xlsx"
    else:
        output = ask("Output file", required=False)

    # ── Build and run ─────────────────────────────────────────────────────────
    args = ["--query", str(query_path)] + var_args + time_args
    if region:         args += ["--region",     region]
    if log_group:      args += ["--log-group",  log_group]
    if limit != "1000": args += ["--limit",     limit]
    if output:         args += ["--output",     output]

    _run("log_insights.py", args)

    # After a CID_Search run, offer to generate the journey map
    if "CID" in ph_values and output and output.endswith(".xlsx"):
        if ask_bool("Generate journey map from this xlsx?"):
            map_output = output.replace(".xlsx", "_journey.html")
            _run("cid_journey.py", [output, "--output", map_output])


# ── Tool: CID Journey ─────────────────────────────────────────────────────────


def tool_cid_journey():
    tool_runner("CID Journey", "cid_journey.py", CID_JOURNEY_QUESTIONS, connect_tool=False)


# ── Tool: Agent Activity ──────────────────────────────────────────────────────

NAMED_PERIODS_AA = ["today", "yesterday", "this-week", "last-week", "this-month", "last-month"]

def tool_agent_activity():
    _header("Agent Activity")
    iid, region, profile = ask_connect_defaults()

    args = connect_args(iid, region, profile)

    use_period = ask_bool("Use a named period?")
    if use_period:
        period = ask_choice("Period", NAMED_PERIODS_AA, default="last-month")
        args += ["--period", period]
    else:
        start = ask_date("Start", ["YYYY-MM-DD"])
        end   = ask_date("End",   ["YYYY-MM-DD"])
        args += ["--start", start, "--end", end]

    if ask_bool("Filter to a specific agent?"):
        login = ask("Agent login")
        args += ["--agent", login]

    output = ask("Output CSV file", required=False)
    if output:
        args += ["--output", output]

    _run("agent_activity.py", args)


# ── Tool: Agent List ──────────────────────────────────────────────────────────


def tool_agent_list():
    tool_runner("Agent List", "agent_list.py", AGENT_LIST_QUESTIONS)


# ── Tool: Routing Profile Audit ────────────────────────────────────────────────

def tool_routing_profile_audit():
    _header("Routing Profile Audit")
    iid, region, profile = ask_connect_defaults()
    name   = ask("Filter by profile name (leave blank for all)", required=False)
    output = ask("CSV output file (leave blank to print)", required=False)

    args = connect_args(iid, region, profile)
    if name:   args += ["--name",   name]
    if output: args += ["--csv",    output]

    _run("routing_profile_audit.py", args)


# ── Tool: Settings ────────────────────────────────────────────────────────────

def tool_settings():
    _header("Settings")
    cfg = ct_config.load()

    print("  Current defaults:\n")
    any_set = False
    for key, label in ct_config.FIELDS:
        val = cfg.get(key) or "(not set)"
        if cfg.get(key):
            any_set = True
        print(f"    {label:<16}  {val}")
    print()

    if not ask_bool("Edit these settings?", default=True):
        return

    new_cfg = {}
    for key, label in ct_config.FIELDS:
        new_cfg[key] = ask(label, required=False, default=cfg.get(key, ""))

    if any_set:
        if not ask_bool("Overwrite existing config?", default=False):
            print("  Cancelled.")
            return

    ct_config.save(new_cfg)
    _cfg.update(new_cfg)
    print(f"  \033[90mSaved to {ct_config.CONFIG_FILE}\033[0m")


# ── Dispatch ──────────────────────────────────────────────────────────────────

# Each tool entry: (display_name, runner_fn, tooltip)
GROUPS = [
    ("Contacts", [
        ("Contacts Handled",   tool_contacts_handled,  "Sum CONTACTS_HANDLED across all queues for a month"),
        ("Contact Inspect",    tool_contact_inspect,   "Full deep-dive: attributes, Lens analysis, transfer chain"),
        ("Contact Timeline",   tool_contact_timeline,  "Chronological flow blocks, Lambda calls, and contact milestones"),
        ("Contact Diff",       tool_contact_diff,      "Side-by-side diff of two contacts: core fields, attributes, and Lens"),
        ("Contact Search",     tool_contact_search,    "Search contacts by date, channel, agent, queue, or attribute"),
        ("Contact Recordings", tool_contact_recordings,"S3 locations and presigned URLs for recordings and transcripts"),
        ("Contact Logs",       tool_contact_logs,      "Download CloudWatch flow-execution logs for a contact ID"),
        ("Lambda Tracer",      tool_lambda_tracer,     "Trace Lambda invocations and fetch execution logs for a contact"),
    ]),
    ("Flows", [
        ("Export Flow",        tool_export_flow,       "Export a contact flow definition to JSON by name"),
        ("Flow to Chart",      tool_flow_to_chart,     "Convert an exported flow JSON to a visual flowchart"),
    ]),
    ("Log Insights", [
        ("Log Insights",       tool_log_insights,      "Run CloudWatch Logs Insights queries with variable substitution"),
        ("CID Journey",        tool_cid_journey,       "Visual journey map from a CID_Search xlsx export"),
    ]),
    ("Agents", [
        ("Agent Activity",        tool_agent_activity,       "Agent handle time and activity report by date range"),
        ("Agent List",            tool_agent_list,           "List agents with routing profile, hierarchy, and security profiles"),
        ("Routing Profile Audit", tool_routing_profile_audit,"Per-profile queue assignments, agent counts, and anomalies"),
    ]),
    ("Settings", [
        ("Settings",           tool_settings,          "View and edit saved instance ID, region, and profile defaults"),
    ]),
]


# ── Dependency check ──────────────────────────────────────────────────────────

def _check_dependencies():
    """Check runtime dependencies and AWS credentials before launching the menu.

    - Auto-installs python-dateutil and openpyxl if missing.
    - Prints actionable instructions for anything it can't fix.
    - Hard-exits on missing critical deps; credential issues are warnings only.
    """
    errors: list[tuple[str, str]] = []    # (problem, fix instruction)
    warnings: list[str] = []

    # Python version
    if sys.version_info < (3, 8):
        errors.append((
            f"Python 3.8+ required (found {sys.version.split()[0]})",
            "Upgrade Python or use AWS CloudShell.",
        ))

    # boto3 / botocore — pre-installed in CloudShell; may be absent locally
    try:
        import boto3      # noqa: F401
        import botocore   # noqa: F401
    except ImportError:
        errors.append((
            "boto3 / botocore not installed",
            "pip install boto3 --user",
        ))
        boto3 = None  # type: ignore[assignment]

    # boto3 version — upgrade if below 1.35.0 to ensure full Connect API coverage
    _MIN_BOTO3 = (1, 35, 0)
    if boto3 is not None:
        try:
            _current = tuple(int(x) for x in boto3.__version__.split(".")[:3])
        except Exception:
            _current = (0, 0, 0)
        if _current < _MIN_BOTO3:
            print(f"  boto3 {boto3.__version__} is outdated — upgrading...", flush=True)
            rc = subprocess.call(
                [sys.executable, "-m", "pip", "install", "--upgrade", "--user", "boto3"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if rc == 0:
                print("  boto3 upgraded — restarting...", flush=True)
                import os
                os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                print("  Warning: boto3 upgrade failed. Some features may not be available.", flush=True)

    # python-dateutil — auto-install if missing
    try:
        import dateutil   # noqa: F401
    except ImportError:
        print("  python-dateutil not found — installing...", flush=True)
        rc = subprocess.call(
            [sys.executable, "-m", "pip", "install", "--user", "python-dateutil"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if rc == 0:
            # Ensure the newly installed package is importable this session
            import site
            if hasattr(site, "getusersitepackages"):
                user_site = site.getusersitepackages()
                if user_site not in sys.path:
                    sys.path.append(user_site)
            print("  python-dateutil installed successfully.", flush=True)
        else:
            errors.append((
                "python-dateutil could not be installed automatically",
                "pip install python-dateutil --user",
            ))

    # openpyxl — required by log_insights.py and cid_journey.py; auto-install if missing
    try:
        import openpyxl   # noqa: F401
    except ImportError:
        print("  openpyxl not found — installing...", flush=True)
        rc = subprocess.call(
            [sys.executable, "-m", "pip", "install", "--user", "openpyxl"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if rc == 0:
            import site
            if hasattr(site, "getusersitepackages"):
                user_site = site.getusersitepackages()
                if user_site not in sys.path:
                    sys.path.append(user_site)
            print("  openpyxl installed successfully.", flush=True)
        else:
            errors.append((
                "openpyxl could not be installed automatically",
                "pip install openpyxl --user",
            ))

    # ct_config.py — must live alongside connectToolbox.py
    if not (SCRIPT_DIR / "ct_config.py").exists():
        errors.append((
            "ct_config.py not found in script directory",
            f"Ensure all scripts are in the same folder: {SCRIPT_DIR}",
        ))

    # AWS credentials — warning only; user may supply --profile per tool
    try:
        import boto3
        creds = boto3.Session().get_credentials()
        if creds is None:
            warnings.append(
                "No AWS credentials detected — tools will fail unless you pass --profile. "
                "Run 'aws configure' or open AWS CloudShell."
            )
    except Exception:
        pass

    if not errors and not warnings:
        return  # all good — silent

    print()
    for problem, fix in errors:
        print(f"  \033[31m✗  {problem}\033[0m")
        print(f"     → {fix}")
    for msg in warnings:
        print(f"  \033[33m⚠  {msg}\033[0m")
    print()

    if errors:
        sys.exit(1)

    # Warnings only — pause so the user sees them before the menu appears
    input("  Press Enter to continue anyway…")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    _check_dependencies()
    clear_screen()
    group_names = [g[0] for g in GROUPS]
    try:
        while True:
            group_idx = pick_menu(TITLE, group_names, quit_label="quit")
            if group_idx is None:
                clear_screen()
                print("\n  Goodbye.\n")
                break
            group_name, tools = GROUPS[group_idx]
            tool_names = [t[0] for t in tools]
            tool_descs = [t[2] if len(t) > 2 else "" for t in tools]
            while True:
                tool_idx = pick_menu(
                    f"{TITLE}  ›  {group_name}",
                    tool_names,
                    quit_label="back",
                    descriptions=tool_descs,
                )
                if tool_idx is None:
                    break
                try:
                    tools[tool_idx][1]()
                except GoBack:
                    pass  # return to group submenu
    except (KeyboardInterrupt, EOFError):
        clear_screen()
        print("\n  Goodbye.\n")


if __name__ == "__main__":
    main()
