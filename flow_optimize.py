#!/usr/bin/env python3
"""flow_optimize.py — Rule-based optimization suggestions for Amazon Connect flows.

Checks flows for UX, reliability, structure, and maintainability issues
that go beyond the hard errors caught by flow_scan.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

try:
    import ct_snapshot as _ct_snapshot
except ImportError:
    _ct_snapshot = None

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

_MAN = """\
NAME
    flow_optimize.py — Rule-based optimization suggestions for Amazon Connect flows

SYNOPSIS
    python flow_optimize.py FLOW_JSON [OPTIONS]
    python flow_optimize.py --instance-id UUID --name NAME [OPTIONS]
    python flow_optimize.py --instance-id UUID --all [OPTIONS]

DESCRIPTION
    Analyses contact flow content for best-practice violations, UX anti-patterns,
    reliability gaps, and maintainability issues. Complements flow_scan.py, which
    catches hard errors (broken references, dead ends). This tool catches softer
    problems that won't crash a flow but will hurt caller experience or make the
    flow harder to maintain.

    Suggestions are grouped into four categories:
      UX            Caller experience issues (menu depth, missing retry paths)
      Reliability   Missing staffing/hours checks, unhandled error paths
      Structure     Flow size, complexity, sequential Lambda calls
      Maintainability  Repeated prompts, flows that could be modularised

OPTIONS
    FLOW_JSON
        Local exported flow JSON file (from export_flow.py). Mutually exclusive with --all.

    --instance-id UUID
        Amazon Connect instance UUID. Required with --name or --all.

    --name NAME
        Flow name to analyse from a live instance (case-insensitive substring).

    --all
        Analyse all flows in the instance.

    --type TYPE
        Filter by flow type when using --all (e.g. CONTACT_FLOW).

    --json
        Emit raw JSON with suggestion details.

    --region REGION
    --profile NAME

EXAMPLES
    python flow_optimize.py Main_IVR.json
    python flow_optimize.py --instance-id <UUID> --name "Main IVR" --region us-east-1
    python flow_optimize.py --instance-id <UUID> --all
    python flow_optimize.py --instance-id <UUID> --all --type CONTACT_FLOW
    python flow_optimize.py --instance-id <UUID> --all --json | jq '.flows[] | select(.suggestion_count > 0)'

IAM PERMISSIONS
    connect:ListContactFlows
    connect:DescribeContactFlow
"""


# ── Suggestion ────────────────────────────────────────────────────────────────

class Suggestion(NamedTuple):
    level:      str   # WARN | SUGGEST
    category:   str   # ux | reliability | structure | maintainability
    block_id:   str   # action Identifier, or "" for flow-level
    block_type: str
    detail:     str


_LEVEL_COLOURS = {
    "WARN":    "\033[33m[WARN   ]\033[0m",
    "SUGGEST": "\033[36m[SUGGEST]\033[0m",
}

_CAT_LABELS = {
    "ux":              "UX",
    "reliability":     "Reliability",
    "structure":       "Structure",
    "maintainability": "Maintainability",
}


# ── Analyser ──────────────────────────────────────────────────────────────────

_LARGE_FLOW_THRESHOLD   = 40
_MAX_MENU_OPTIONS       = 5
_MIN_DUPLICATE_TEXT_LEN = 10   # ignore very short strings
_DUPLICATE_MIN_COUNT    = 3    # text must appear in this many blocks to flag


def analyse_flow(content: dict, flow_type: str = "") -> list:
    suggestions: list[Suggestion] = []
    raw     = content.get("Actions") or []
    actions = {a["Identifier"]: a for a in raw if "Identifier" in a}

    if not actions:
        return suggestions

    # Build type → list[id] index and NextAction graph
    by_type:   dict = defaultdict(list)
    successors: dict = {}   # id → default NextAction id

    for aid, action in actions.items():
        atype = action.get("Type", "")
        by_type[atype].append(aid)
        nxt = (action.get("Transitions") or {}).get("NextAction") or ""
        if nxt:
            successors[aid] = nxt

    # ── UX checks ────────────────────────────────────────────────────────────

    for aid in by_type.get("GetUserInput", []):
        action = actions[aid]
        trans  = action.get("Transitions") or {}
        conds  = trans.get("Conditions") or []
        errors = trans.get("Errors") or []

        # 1. Too many menu options
        if len(conds) > _MAX_MENU_OPTIONS:
            suggestions.append(Suggestion(
                "WARN", "ux", aid, "GetUserInput",
                f"{len(conds)} menu options — consider reducing to {_MAX_MENU_OPTIONS} or fewer; "
                "callers struggle with long option lists",
            ))

        # 2. No error branch (NoMatch / timeout)
        has_error_branch = any(e.get("NextAction") for e in errors)
        if not has_error_branch:
            suggestions.append(Suggestion(
                "WARN", "ux", aid, "GetUserInput",
                "No error handler — callers who press an invalid key or time out have no path forward",
            ))

    # ── Reliability checks ────────────────────────────────────────────────────

    # 3. TransferToQueue present but no CheckStaffingStatus anywhere
    has_transfer = bool(by_type.get("TransferContactToQueue"))
    has_staffing = bool(by_type.get("CheckStaffingStatus"))
    if has_transfer and not has_staffing:
        suggestions.append(Suggestion(
            "SUGGEST", "reliability", "", "",
            "Flow transfers to a queue but never checks staffing — callers may be transferred "
            "to a queue with no agents available",
        ))

    # 4. No CheckHoursOfOperation in a CONTACT_FLOW (entry-point flow)
    if flow_type in ("CONTACT_FLOW", "") and not by_type.get("CheckHoursOfOperation"):
        # Only flag if the flow actually routes somewhere (has a transfer or set-queue)
        has_routing = bool(by_type.get("TransferContactToQueue") or by_type.get("SetQueue"))
        if has_routing:
            suggestions.append(Suggestion(
                "SUGGEST", "reliability", "", "",
                "No hours-of-operation check — callers may be routed to a queue outside business hours",
            ))

    # ── Structure checks ──────────────────────────────────────────────────────

    # 5. Large flow
    if len(actions) > _LARGE_FLOW_THRESHOLD:
        suggestions.append(Suggestion(
            "SUGGEST", "structure", "", "",
            f"Flow has {len(actions)} blocks (>{_LARGE_FLOW_THRESHOLD}) — "
            "consider splitting into sub-flows via InvokeFlowModule for easier maintenance",
        ))

    # 6. Sequential Lambda calls (back-to-back on the default path)
    lambda_ids = set(by_type.get("InvokeLambdaFunction", []))
    for aid in lambda_ids:
        nxt = successors.get(aid, "")
        if nxt and nxt in lambda_ids:
            fn1 = (actions[aid].get("Parameters") or {}).get("LambdaFunctionARN", "")
            fn2 = (actions[nxt].get("Parameters") or {}).get("LambdaFunctionARN", "")
            name1 = fn1.split(":")[-1] if fn1 else aid
            name2 = fn2.split(":")[-1] if fn2 else nxt
            suggestions.append(Suggestion(
                "SUGGEST", "structure", aid, "InvokeLambdaFunction",
                f"Back-to-back Lambda calls: {name1!r} → {name2!r} — "
                "consider combining into a single function to reduce latency and API call count",
            ))

    # ── Maintainability checks ────────────────────────────────────────────────

    # 7. Duplicate prompt text
    text_sources: dict = defaultdict(list)
    for aid, action in actions.items():
        params = action.get("Parameters") or {}
        text   = params.get("Text") or ""
        if isinstance(text, str) and len(text) >= _MIN_DUPLICATE_TEXT_LEN:
            text_sources[text].append(aid)

    for text, aids in text_sources.items():
        if len(aids) >= _DUPLICATE_MIN_COUNT:
            snippet = text[:60] + ("…" if len(text) > 60 else "")
            suggestions.append(Suggestion(
                "SUGGEST", "maintainability", aids[0], "",
                f"Prompt text {snippet!r} appears in {len(aids)} blocks — "
                "consider storing as a shared prompt resource",
            ))

    return suggestions


# ── Flow content loader ───────────────────────────────────────────────────────

def load_content_from_file(path: str) -> tuple:
    """Returns (flow_name, flow_type, content_dict)."""
    resolved = Path(path).expanduser()
    try:
        with open(resolved, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: file not found: {resolved}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if "content" in data and "Actions" in (data.get("content") or {}):
        meta    = data.get("metadata") or {}
        name    = meta.get("name") or path
        ftype   = meta.get("type") or ""
        content = data["content"]
    elif "Actions" in data:
        name, ftype, content = path, "", data
    else:
        print("Error: file does not look like a contact flow (no 'Actions' array).",
              file=sys.stderr)
        sys.exit(1)

    return name, ftype, content


# ── AWS helpers ───────────────────────────────────────────────────────────────

def make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


def list_all_flows(client, instance_id, flow_types=None):
    flows, token = [], None
    while True:
        kwargs = dict(InstanceId=instance_id, MaxResults=100)
        if flow_types:
            kwargs["ContactFlowTypes"] = flow_types
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_contact_flows(**kwargs)
        except ClientError as e:
            print(f"Error listing flows: {e.response['Error']['Message']}", file=sys.stderr)
            sys.exit(1)
        flows.extend(resp.get("ContactFlowSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return flows


def describe_flow_content(client, instance_id, flow_id) -> dict | None:
    try:
        raw     = client.describe_contact_flow(InstanceId=instance_id, ContactFlowId=flow_id)
        content = raw["ContactFlow"].get("Content") or ""
        return json.loads(content) if isinstance(content, str) else content
    except (ClientError, json.JSONDecodeError):
        return None


# ── Output ────────────────────────────────────────────────────────────────────

def _hr(width=72):
    print("  " + "─" * width)


def print_flow_result(flow_name: str, n_blocks: int, suggestions: list):
    by_cat: dict = defaultdict(list)
    for s in suggestions:
        by_cat[s.category].append(s)

    _hr()
    print(f"  FLOW OPTIMIZE   {flow_name}")
    _hr()
    n_warn    = sum(1 for s in suggestions if s.level == "WARN")
    n_suggest = sum(1 for s in suggestions if s.level == "SUGGEST")
    print(f"  {n_blocks} block(s)   ", end="")
    if not suggestions:
        print("\033[32m✓ No suggestions\033[0m")
        print()
        return
    print(f"{len(suggestions)} suggestion(s)   "
          f"\033[33m{n_warn} WARN\033[0m  \033[36m{n_suggest} SUGGEST\033[0m")
    print()

    for cat in ("ux", "reliability", "structure", "maintainability"):
        items = by_cat.get(cat, [])
        if not items:
            continue
        print(f"  {_CAT_LABELS[cat]}")
        print(f"  {'─' * 68}")
        for s in items:
            level_str = _LEVEL_COLOURS[s.level]
            if s.block_id:
                loc = f'  "{s.block_id}"' if s.block_type else f"  block {s.block_id[:8]}…"
            else:
                loc = "  (flow level)"
            print(f"  {level_str}  {loc}")
            # Word-wrap detail to 66 chars
            words = s.detail.split()
            line, lines = [], []
            for w in words:
                if sum(len(x) + 1 for x in line) + len(w) > 66:
                    lines.append(" ".join(line))
                    line = [w]
                else:
                    line.append(w)
            if line:
                lines.append(" ".join(line))
            for ln in lines:
                print(f"             {ln}")
            print()

    _hr()
    print()


# ── JSON serialisation ────────────────────────────────────────────────────────

def _sug_to_dict(s: Suggestion) -> dict:
    return {"level": s.level, "category": s.category,
            "block_id": s.block_id, "block_type": s.block_type, "detail": s.detail}


def _to_json(flow_name: str, n_blocks: int, suggestions: list) -> dict:
    return {
        "flow":             flow_name,
        "block_count":      n_blocks,
        "suggestion_count": len(suggestions),
        "warns":            sum(1 for s in suggestions if s.level == "WARN"),
        "suggestions":      [_sug_to_dict(s) for s in suggestions],
    }


def _bulk_to_json(results: list) -> dict:
    flows = [_to_json(n, b, s) for n, b, s in results]
    return {
        "flow_count":             len(flows),
        "flows_with_suggestions": sum(1 for f in flows if f["suggestion_count"] > 0),
        "total_suggestions":      sum(f["suggestion_count"] for f in flows),
        "flows":                  flows,
    }


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Rule-based optimization suggestions for Amazon Connect flows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s Main_IVR.json
  %(prog)s --instance-id <UUID> --name "Main IVR" --region us-east-1
  %(prog)s --instance-id <UUID> --all
  %(prog)s --instance-id <UUID> --all --type CONTACT_FLOW --json
        """,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("flow_file", nargs="?", metavar="FLOW_JSON")
    src.add_argument("--all", action="store_true")

    p.add_argument("--instance-id", default=None, metavar="UUID")
    p.add_argument("--name",    default=None, metavar="NAME")
    p.add_argument("--type",    default=None, metavar="TYPE")
    p.add_argument("--region",  default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--json",    action="store_true", dest="output_json")

    args = p.parse_args()
    if not args.flow_file and not args.all and not args.name:
        p.error("provide a FLOW_JSON file, --name <name>, or --all")
    if (args.all or args.name) and not args.instance_id:
        p.error("--instance-id is required with --name and --all")
    if args.flow_file and args.instance_id:
        p.error("--instance-id cannot be combined with a local FLOW_JSON file")
    return args


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args = parse_args()

    # ── Local file ────────────────────────────────────────────────────────────
    if args.flow_file:
        name, ftype, content = load_content_from_file(args.flow_file)
        n_blocks    = len(content.get("Actions") or [])
        suggestions = analyse_flow(content, ftype)
        if args.output_json:
            print(json.dumps(_to_json(name, n_blocks, suggestions), indent=2))
        else:
            print_flow_result(name, n_blocks, suggestions)
        return

    client = make_client(args.region, args.profile)

    # ── Single flow by name ───────────────────────────────────────────────────
    if args.name and not args.all:
        print("  Listing flows...", file=sys.stderr)
        all_flows  = list_all_flows(client, args.instance_id)
        name_lower = args.name.lower()
        matches    = [f for f in all_flows if name_lower in f["Name"].lower()]

        if not matches:
            print(f"No flows found matching {args.name!r}.", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"Found {len(matches)} flows matching {args.name!r} — be more specific:",
                  file=sys.stderr)
            for f in matches:
                print(f"  {f['Name']!r}", file=sys.stderr)
            sys.exit(1)

        summary = matches[0]
        print(f"  Analysing '{summary['Name']}'...", file=sys.stderr)
        content = describe_flow_content(client, args.instance_id, summary["Id"])
        if content is None:
            print("Error: could not load flow content.", file=sys.stderr)
            sys.exit(1)

        n_blocks    = len(content.get("Actions") or [])
        suggestions = analyse_flow(content, summary.get("ContactFlowType", ""))
        if args.output_json:
            print(json.dumps(_to_json(summary["Name"], n_blocks, suggestions), indent=2))
        else:
            print_flow_result(summary["Name"], n_blocks, suggestions)
        return

    # ── Bulk mode ─────────────────────────────────────────────────────────────
    flow_types = [args.type] if args.type else None
    print("  Listing flows...", file=sys.stderr)
    all_flows = list_all_flows(client, args.instance_id, flow_types)
    if not all_flows:
        print("No flows found.", file=sys.stderr)
        sys.exit(0)

    print(f"  Analysing {len(all_flows)} flow(s)...", file=sys.stderr)
    results = []
    for i, summary in enumerate(all_flows, 1):
        print(f"  [{i}/{len(all_flows)}] {summary['Name']}", file=sys.stderr)
        content = describe_flow_content(client, args.instance_id, summary["Id"])
        if content is None:
            continue
        n_blocks    = len(content.get("Actions") or [])
        suggestions = analyse_flow(content, summary.get("ContactFlowType", ""))
        results.append((summary["Name"], n_blocks, suggestions))

    if args.output_json:
        print(json.dumps(_bulk_to_json(results), indent=2))
        return

    for name, n_blocks, suggestions in sorted(results, key=lambda r: r[0].lower()):
        print_flow_result(name, n_blocks, suggestions)


if __name__ == "__main__":
    main()
