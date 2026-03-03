#!/usr/bin/env python3
"""menu.py — Interactive launcher for Amazon Connect Tools."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
TITLE      = "Amazon Connect Tools"

TOOLS = [
    "Contacts Handled",
    "Contact Inspect",
    "Export Flow",
    "Flow to Chart",
]


# ── Raw keypress reader (cross-platform) ──────────────────────────────────────

if sys.platform == "win32":
    import msvcrt

    def getch() -> bytes:
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):   # special key prefix on Windows
            ch = b"\xe0" + msvcrt.getch()
        return ch

    UP   = b"\xe0H"
    DOWN = b"\xe0P"
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

    UP   = b"\x1b[A"
    DOWN = b"\x1b[B"
    CLEAR = "clear"

QUIT = (b"q", b"Q", b"\x03")   # q, Q, Ctrl-C


# ── Main menu ─────────────────────────────────────────────────────────────────

def _draw_menu(selected: int):
    os.system(CLEAR)
    print(f"\n  {TITLE}")
    print("  " + "─" * 36)
    print()
    for i, name in enumerate(TOOLS):
        if i == selected:
            print(f"  \033[7m  {i + 1}.  {name:<22}\033[0m")
        else:
            print(f"     {i + 1}.  {name}")
    print()
    print("  \033[90m[↑↓] navigate  [Enter / 1-4] select  [q] quit\033[0m")


def main_menu() -> int | None:
    """Show the arrow-key menu. Returns 0-based tool index or None to quit."""
    selected = 0
    while True:
        _draw_menu(selected)
        key = getch()
        if key in QUIT:
            return None
        if key == UP:
            selected = (selected - 1) % len(TOOLS)
        elif key == DOWN:
            selected = (selected + 1) % len(TOOLS)
        elif key in (b"\r", b"\n"):
            return selected
        elif key in (b"1", b"2", b"3", b"4"):
            return int(key.decode()) - 1


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _header(tool_name: str):
    os.system(CLEAR)
    print(f"\n  {TITLE}  ›  {tool_name}")
    print("  " + "─" * 40)
    print()


def ask(label: str, required: bool = True, default: str = "") -> str:
    hint = f"[{default}]" if default else ("required" if required else "optional")
    while True:
        val = input(f"  {label} ({hint}): ").strip()
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
    return default if not val else val in ("y", "yes")


# ── Tool launchers ────────────────────────────────────────────────────────────

def _run(script: str, args: list[str]):
    """Execute a tool script and wait; then pause before returning to menu."""
    print()
    print("  " + "─" * 40)
    print()
    subprocess.run([sys.executable, str(SCRIPT_DIR / script)] + args)
    print()
    print("  " + "─" * 40)
    input("  Press Enter to return to menu…")


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


def tool_flow_to_chart():
    _header("Flow to Chart")
    flow_file = ask("Flow JSON file path")
    fmt       = ask_choice("Format", ["html", "mermaid", "dot"], default="html")
    output    = ask("Output file", required=False)

    args = [flow_file, "--format", fmt]
    if output:
        args += ["--output", output]

    _run("flow_to_chart.py", args)


RUNNERS = [
    tool_contacts_handled,
    tool_contact_inspect,
    tool_export_flow,
    tool_flow_to_chart,
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
            RUNNERS[choice]()
    except (KeyboardInterrupt, EOFError):
        os.system(CLEAR)
        print("\n  Goodbye.\n")


if __name__ == "__main__":
    main()
