#!/usr/bin/env python3
"""connectToolbox.py — Interactive launcher for Amazon Connect Tools."""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent
QUERIES_DIR = SCRIPT_DIR / "queries"
TITLE       = "Amazon Connect Tools"
LOG_FILE    = Path.home() / "logs" / "connecttools.log"

TOOLS = [
    "Contacts Handled",
    "Contact Inspect",
    "Contact Search",
    "Export Flow",
    "Flow to Chart",
    "Log Insights",
    "CID Journey",
    "Agent Activity",
]

_PLACEHOLDER_RE = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")


class GoBack(Exception):
    """Raised from any prompt when the user types '..' to return to the main menu."""


# ── Raw keypress reader (cross-platform) ──────────────────────────────────────

if sys.platform == "win32":
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


# ── Generic arrow-key menu ────────────────────────────────────────────────────

def pick_menu(title: str, options: list[str], quit_label: str = "back") -> int | None:
    """Arrow-key or number selection. Returns 0-based index or None to go back/quit."""
    selected = 0
    n = len(options)
    while True:
        os.system(CLEAR)
        print(f"\n  {title}")
        print("  " + "─" * max(40, len(title) + 2))
        print()
        for i, name in enumerate(options):
            num = str(i + 1) if i < 9 else " "
            if i == selected:
                print(f"  \033[7m  {num}.  {name:<30}\033[0m")
            else:
                print(f"     {num}.  {name}")
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


def main_menu() -> int | None:
    return pick_menu(TITLE, TOOLS, quit_label="quit")


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _header(*crumbs: str):
    os.system(CLEAR)
    print(f"\n  {TITLE}  ›  {'  ›  '.join(crumbs)}")
    print("  " + "─" * 40)
    print(f"  \033[90m  type .. at any prompt to go back\033[0m")
    print()


def ask(label: str, required: bool = True, default: str = "") -> str:
    hint = f"[{default}]" if default else ("required" if required else "optional")
    while True:
        val = input(f"  {label} ({hint}): ").strip()
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
        val = input(f"  Choice [{default}]: ").strip()
        if val == "..":
            raise GoBack
        if not val:
            return default
        if val.isdigit() and 1 <= int(val) <= len(choices):
            return choices[int(val) - 1]
        if val in choices:
            return val
        print(f"  \033[33m  Enter a number 1–{len(choices)}\033[0m")


def ask_bool(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    val  = input(f"  {label} [{hint}]: ").strip().lower()
    if val == "..":
        raise GoBack
    return default if not val else val in ("y", "yes")


# ── Tool runner + logging ─────────────────────────────────────────────────────

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
    input("  Press Enter to return to menu…")


# ── Tool: Contacts Handled ────────────────────────────────────────────────────

def tool_contacts_handled():
    _header("Contacts Handled")
    iid    = ask("Instance ID")
    month  = ask("Month (YYYY-MM)", required=False)
    region = ask("Region",          required=False)
    tz     = ask("Timezone",        required=False)

    args = ["--instance-id", iid]
    if month:  args += ["--month",    month]
    if region: args += ["--region",   region]
    if tz:     args += ["--timezone", tz]

    _run("contacts_handled.py", args)


# ── Tool: Contact Inspect ─────────────────────────────────────────────────────

def tool_contact_inspect():
    _header("Contact Inspect")
    iid    = ask("Instance ID")
    cid    = ask("Contact ID")
    region = ask("Region", required=False)
    trans  = ask_bool("Include full transcript?")

    args = ["--instance-id", iid, "--contact-id", cid]
    if region: args += ["--region", region]
    if trans:  args += ["--transcript"]

    _run("contact_inspect.py", args)


# ── Tool: Contact Search ──────────────────────────────────────────────────────

def tool_contact_search():
    _header("Contact Search")
    iid    = ask("Instance ID")
    start  = ask("Start (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    end    = ask("End   (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    region = ask("Region", required=False)

    args = ["--instance-id", iid, "--start", start, "--end", end]
    if region:
        args += ["--region", region]

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


# ── Tool: Export Flow ─────────────────────────────────────────────────────────

def tool_export_flow():
    _header("Export Flow")
    iid    = ask("Instance ID")
    region = ask("Region", required=False)

    if ask_bool("List available flows first?"):
        list_args = ["--instance-id", iid, "--list"]
        if region:
            list_args += ["--region", region]
        print()
        subprocess.run([sys.executable, str(SCRIPT_DIR / "export_flow.py")] + list_args)
        print()

    name   = ask("Flow name")
    exact  = ask_bool("Exact name match?")
    output = ask("Output file", required=False)

    args = ["--instance-id", iid, "--name", name]
    if region: args += ["--region", region]
    if exact:  args += ["--exact"]
    if output: args += ["--output", output]

    _run("export_flow.py", args)


# ── Tool: Flow to Chart ───────────────────────────────────────────────────────

def tool_flow_to_chart():
    _header("Flow to Chart")
    flow_file = ask("Flow JSON file path")
    fmt       = ask_choice("Format", ["html", "mermaid", "dot"], default="html")
    output    = ask("Output file", required=False)

    args = [flow_file, "--format", fmt]
    if output:
        args += ["--output", output]

    _run("flow_to_chart.py", args)


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
        val = input("  Exclude # (or blank to finish): ").strip()
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
        r"^\s*\|\s*display\s+.+$", new_display, query,
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
            line = input()
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
        start = ask("Start (YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
        end   = ask("End", required=False)
        time_args = ["--start", start]
        if end:
            time_args += ["--end", end]
    else:
        last = ask("Duration", default="24h")
        time_args = ["--last", last]

    # ── Other options ─────────────────────────────────────────────────────────
    region    = ask("Region",     required=False)
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
    _header("CID Journey")
    xlsx_file = ask("xlsx file path (from CID_Search Log Insights run)")
    output    = ask("Output HTML file", required=False)

    args = [xlsx_file]
    if output:
        args += ["--output", output]

    _run("cid_journey.py", args)


# ── Tool: Agent Activity ──────────────────────────────────────────────────────

NAMED_PERIODS_AA = ["today", "yesterday", "this-week", "last-week", "this-month", "last-month"]

def tool_agent_activity():
    _header("Agent Activity")
    iid    = ask("Instance ID")
    region = ask("Region", required=False)

    args = ["--instance-id", iid]
    if region:
        args += ["--region", region]

    use_period = ask_bool("Use a named period?")
    if use_period:
        period = ask_choice("Period", NAMED_PERIODS_AA, default="last-month")
        args += ["--period", period]
    else:
        start = ask("Start (YYYY-MM-DD)")
        end   = ask("End   (YYYY-MM-DD)")
        args += ["--start", start, "--end", end]

    if ask_bool("Filter to a specific agent?"):
        login = ask("Agent login")
        args += ["--agent", login]

    output = ask("Output CSV file", required=False)
    if output:
        args += ["--output", output]

    _run("agent_activity.py", args)


# ── Dispatch ──────────────────────────────────────────────────────────────────

RUNNERS = [
    tool_contacts_handled,
    tool_contact_inspect,
    tool_contact_search,
    tool_export_flow,
    tool_flow_to_chart,
    tool_log_insights,
    tool_cid_journey,
    tool_agent_activity,
]


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    os.system(CLEAR)
    try:
        while True:
            choice = main_menu()
            if choice is None:
                os.system(CLEAR)
                print("\n  Goodbye.\n")
                break
            try:
                RUNNERS[choice]()
            except GoBack:
                pass  # return to main menu
    except (KeyboardInterrupt, EOFError):
        os.system(CLEAR)
        print("\n  Goodbye.\n")


if __name__ == "__main__":
    main()
