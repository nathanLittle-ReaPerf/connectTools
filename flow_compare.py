#!/usr/bin/env python3
"""flow_compare.py — Diff two exported Amazon Connect flow JSONs.

Compares blocks side-by-side: added, removed, and modified (with
per-field diffs of Parameters and Transitions). No AWS calls required.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_MAN = """\
NAME
    flow_compare.py — Diff two exported Amazon Connect contact flow JSONs

SYNOPSIS
    python flow_compare.py LEFT.json RIGHT.json [OPTIONS]

DESCRIPTION
    Compares two exported contact flow JSON files (from export_flow.py or raw)
    and reports blocks that were added, removed, or modified between them.
    For modified blocks, shows exactly which Parameters and Transitions fields
    changed and what the old vs new values are.

    No AWS calls are made — works entirely on local files.

OPTIONS
    LEFT.json   Older / baseline flow export.
    RIGHT.json  Newer / changed flow export.

    --json      Print results as JSON to stdout.

EXAMPLES
    # Compare two versions of a flow
    python flow_compare.py Main_IVR_v1.json Main_IVR_v2.json

    # JSON output
    python flow_compare.py old.json new.json --json | jq '.modified[].changes'

NOTES
    Accepts both the export_flow.py envelope format ({"metadata":..., "content":...})
    and raw flow content JSON directly.
    Blocks are matched by Identifier. List fields (Conditions, Errors) are
    compared by index — reordering is reported as a modification.
"""

_MISSING = object()   # sentinel for absent fields


# ── File loading ────────────────────────────────────────────────────────────────

def load_flow(path: str) -> tuple:
    """Load a flow JSON. Returns (label, metadata_dict, content_dict)."""
    p = Path(path).expanduser()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: file not found: {p}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if "content" in data and "Actions" in (data.get("content") or {}):
        meta    = data.get("metadata") or {}
        content = data["content"]
    elif "Actions" in data:
        meta    = {}
        content = data
    else:
        print(f"Error: {path} does not look like a contact flow (no 'Actions' array).",
              file=sys.stderr)
        sys.exit(1)

    label = meta.get("name") or p.stem
    return label, meta, content


# ── Diff helpers ─────────────────────────────────────────────────────────────────

def _flatten(obj, prefix: str = "") -> dict:
    """Recursively flatten a nested dict/list into {dotted[i].path: value}."""
    result = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{prefix}.{k}" if prefix else k
            result.update(_flatten(v, sub))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            result.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        result[prefix] = obj
    return result


def diff_block(left: dict, right: dict) -> list:
    """Return list of (path, left_val, right_val) for all differences."""
    changes = []

    # Type change
    lt, rt = left.get("Type", ""), right.get("Type", "")
    if lt != rt:
        changes.append(("Type", lt, rt))

    # Parameters and Transitions
    for section in ("Parameters", "Transitions"):
        lf = _flatten(left.get(section) or {}, section)
        rf = _flatten(right.get(section) or {}, section)
        for k in sorted(set(lf) | set(rf)):
            lv = lf.get(k, _MISSING)
            rv = rf.get(k, _MISSING)
            if lv != rv:
                changes.append((k, lv, rv))

    return changes


def _fmt_val(v) -> str:
    if v is _MISSING:
        return "[absent]"
    if isinstance(v, str):
        return repr(v)
    return json.dumps(v)


# ── Core comparison ─────────────────────────────────────────────────────────────

def compare_flows(left_content: dict, right_content: dict) -> dict:
    left_actions  = {a["Identifier"]: a for a in (left_content.get("Actions")  or []) if "Identifier" in a}
    right_actions = {a["Identifier"]: a for a in (right_content.get("Actions") or []) if "Identifier" in a}

    left_ids  = set(left_actions)
    right_ids = set(right_actions)

    added   = sorted(right_ids - left_ids)
    removed = sorted(left_ids  - right_ids)
    common  = sorted(left_ids  & right_ids)

    modified  = []
    unchanged = []
    for aid in common:
        changes = diff_block(left_actions[aid], right_actions[aid])
        if changes:
            modified.append({"id": aid, "type": right_actions[aid].get("Type", ""),
                             "left_type": left_actions[aid].get("Type", ""),
                             "changes": [(k, lv, rv) for k, lv, rv in changes]})
        else:
            unchanged.append(aid)

    return {
        "left_start":  left_content.get("StartAction"),
        "right_start": right_content.get("StartAction"),
        "left_actions":  left_actions,
        "right_actions": right_actions,
        "added":     [{"id": i, "type": right_actions[i].get("Type", "")} for i in added],
        "removed":   [{"id": i, "type": left_actions[i].get("Type", "")}  for i in removed],
        "modified":  modified,
        "unchanged": unchanged,
    }


# ── Output ───────────────────────────────────────────────────────────────────────

def _hr(label: str = "", width: int = 72):
    if label:
        pad = width - len(label) - 4
        print(f"  ── {label} {'─' * max(pad, 0)}")
    else:
        print("  " + "─" * width)


def _block_label(bid: str, btype: str, width: int = 30) -> str:
    name = bid if len(bid) <= width else bid[:width - 1] + "…"
    return f'"{name}"  ({btype})'


def print_human(left_label: str, left_meta: dict, left_content: dict,
                right_label: str, right_meta: dict, right_content: dict,
                diff: dict):
    n_left  = len(left_content.get("Actions") or [])
    n_right = len(right_content.get("Actions") or [])

    _hr()
    print("  FLOW COMPARE")
    _hr()

    left_date  = (left_meta.get("exported_at")  or "")[:10]
    right_date = (right_meta.get("exported_at") or "")[:10]
    left_info  = f"{left_label}" + (f"  ·  {left_date}" if left_date else "") + f"  ·  {n_left} blocks"
    right_info = f"{right_label}" + (f"  ·  {right_date}" if right_date else "") + f"  ·  {n_right} blocks"
    print(f"  Left :  {left_info}")
    print(f"  Right:  {right_info}")
    print()

    # Start action
    ls, rs = diff["left_start"], diff["right_start"]
    if ls == rs:
        start_label = ls or "(none)"
        print(f"  Start action: unchanged  →  {start_label!r}")
    else:
        print(f"  Start action changed:")
        print(f"    < {ls!r}")
        print(f"    > {rs!r}")
    print()

    n_add  = len(diff["added"])
    n_rem  = len(diff["removed"])
    n_mod  = len(diff["modified"])
    n_unch = len(diff["unchanged"])
    parts  = []
    if n_add:  parts.append(f"\033[32m{n_add} added\033[0m")
    if n_rem:  parts.append(f"\033[31m{n_rem} removed\033[0m")
    if n_mod:  parts.append(f"\033[33m{n_mod} modified\033[0m")
    parts.append(f"{n_unch} unchanged")
    print("  " + "  ·  ".join(parts))
    print()

    if not (n_add or n_rem or n_mod):
        print("  \033[32m✓ Flows are identical\033[0m")
        print()
        _hr()
        print()
        return

    if n_add:
        _hr("ADDED")
        for item in diff["added"]:
            print(f"  \033[32m+\033[0m  {_block_label(item['id'], item['type'])}")
        print()

    if n_rem:
        _hr("REMOVED")
        for item in diff["removed"]:
            print(f"  \033[31m-\033[0m  {_block_label(item['id'], item['type'])}")
        print()

    if n_mod:
        _hr("MODIFIED")
        for mod in diff["modified"]:
            btype = mod["type"]
            lt    = mod["left_type"]
            type_note = f" ({lt} → {btype})" if lt != btype else f" ({btype})"
            print(f"  \033[33m~\033[0m  \"{mod['id']}\"{type_note}")
            for path, lv, rv in mod["changes"]:
                print(f"       {path}")
                print(f"         \033[31m< {_fmt_val(lv)}\033[0m")
                print(f"         \033[32m> {_fmt_val(rv)}\033[0m")
            print()

    _hr()
    print()


# ── JSON output ──────────────────────────────────────────────────────────────────

def to_json(left_label: str, right_label: str, diff: dict) -> dict:
    def serialise_changes(changes):
        return [
            {"path": k,
             "left":  None if lv is _MISSING else lv,
             "right": None if rv is _MISSING else rv}
            for k, lv, rv in changes
        ]

    return {
        "left":  left_label,
        "right": right_label,
        "start_action_changed": diff["left_start"] != diff["right_start"],
        "left_start":  diff["left_start"],
        "right_start": diff["right_start"],
        "summary": {
            "added":     len(diff["added"]),
            "removed":   len(diff["removed"]),
            "modified":  len(diff["modified"]),
            "unchanged": len(diff["unchanged"]),
        },
        "added":   diff["added"],
        "removed": diff["removed"],
        "modified": [
            {"id": m["id"], "type": m["type"],
             "changes": serialise_changes(m["changes"])}
            for m in diff["modified"]
        ],
    }


# ── Argument parsing ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two exported Amazon Connect contact flow JSONs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s Main_IVR_v1.json Main_IVR_v2.json
  %(prog)s old.json new.json --json | jq '.modified[].changes'
        """,
    )
    p.add_argument("left",  metavar="LEFT.json",  help="Older / baseline flow export")
    p.add_argument("right", metavar="RIGHT.json", help="Newer / changed flow export")
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Print results as JSON to stdout")
    return p.parse_args()


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args = parse_args()

    left_label,  left_meta,  left_content  = load_flow(args.left)
    right_label, right_meta, right_content = load_flow(args.right)

    diff = compare_flows(left_content, right_content)

    if args.output_json:
        print(json.dumps(to_json(left_label, right_label, diff), indent=2))
        return

    print_human(left_label, left_meta, left_content,
                right_label, right_meta, right_content, diff)


if __name__ == "__main__":
    main()
