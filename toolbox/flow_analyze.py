#!/usr/bin/env python3
"""flow_analyze.py — Scan and optimize Amazon Connect contact flows.

Combines flow_scan (hard errors: broken refs, dead ends, missing handlers)
and flow_optimize (soft suggestions: UX, reliability, structure, maintainability)
into a single pass. Default behavior runs both; use --scan or --optimize alone
to restrict to one.
"""

from __future__ import annotations

import argparse
import csv
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
    flow_analyze.py — Scan and optimize Amazon Connect contact flows

SYNOPSIS
    python flow_analyze.py FLOW_JSON [OPTIONS]
    python flow_analyze.py --instance-id UUID --name NAME [OPTIONS]
    python flow_analyze.py --instance-id UUID --all [OPTIONS]

DESCRIPTION
    Runs two analysis passes on one or more contact flows:

      SCAN     Hard errors that will break or confuse contacts: broken block
               references, dead-end blocks, missing error handlers, missing
               default branches, unreachable blocks, empty Lambda ARNs,
               unconfigured queues.

      OPTIMIZE Rule-based suggestions: UX anti-patterns (deep menus, no retry),
               reliability gaps (no hours check, no staffing check), structure
               issues (large flows, back-to-back Lambdas), maintainability
               (duplicate prompt text).

    Default (no mode flag): runs both passes. Use --scan or --optimize alone
    to restrict to one pass.

OPTIONS
    FLOW_JSON
        Local exported flow JSON file (from export_flow.py).

    --instance-id UUID
        Amazon Connect instance UUID. Required with --name or --all.

    --name NAME
        Flow name to analyse (case-insensitive substring match).

    --all
        Analyse all flows in the instance.

    --type TYPE
        Filter by flow type with --all (e.g. CONTACT_FLOW).

    --scan
        Run error scanner only.

    --optimize
        Run optimization checker only.

    --detail
        In bulk (--all) mode, show per-block breakdown for flows with findings.
        Without this flag, a summary table is shown.

    --csv FILE
        Write scan issues to CSV (one row per issue; requires --scan or default mode).

    --json
        Emit raw JSON with all findings.

    --region REGION
    --profile NAME

EXAMPLES
    # Scan + optimize a local flow
    python flow_analyze.py Main_IVR.json

    # Scan only, live instance
    python flow_analyze.py --instance-id <UUID> --name "Main IVR" --scan

    # Full analysis of all flows with detail on flows that have findings
    python flow_analyze.py --instance-id <UUID> --all --detail

    # Bulk JSON, pipe to jq
    python flow_analyze.py --instance-id <UUID> --all --json \\
      | jq '.flows[] | select(.scan.issue_count > 0)'

IAM PERMISSIONS
    connect:ListContactFlows
    connect:DescribeContactFlow
"""


# ═══════════════════════════════════════════════════════════════════════════════
# SCAN — hard error detection
# ═══════════════════════════════════════════════════════════════════════════════

TERMINAL_TYPES = {
    "DisconnectParticipant",
    "TransferContactToQueue",
    "TransferContactToFlow",
    "EndFlowExecution",
}

DECISION_TYPES = {
    "CheckAttribute",
    "CheckContactAttributes",
    "GetUserInput",
    "CheckHoursOfOperation",
    "CheckStaffingStatus",
    "CheckAgentStatus",
}

ERROR_CAPABLE_TYPES = {
    "InvokeLambdaFunction",
    "InvokeFlowModule",
    "TransferContactToQueue",
}


class Issue(NamedTuple):
    severity:   str   # ERROR | WARN
    kind:       str
    block_id:   str
    block_type: str
    detail:     str


_snapshot: dict | None = None


def _short(identifier: str) -> str:
    if _snapshot and _ct_snapshot:
        for rtype in ("flows", "queues", "routing_profiles", "prompts", "quick_connects"):
            name = _ct_snapshot.resolve(_snapshot, rtype, identifier)
            if name:
                return f"{name} ({identifier[:8]}…)"
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}", identifier.lower()):
        return identifier[:8] + "…"
    return identifier[:40]


def scan_flow(content: dict) -> list:
    issues  = []
    raw     = content.get("Actions") or []
    actions = {a["Identifier"]: a for a in raw if "Identifier" in a}
    start_id = content.get("StartAction")

    if not actions:
        return issues

    if start_id and start_id not in actions:
        issues.append(Issue(
            "ERROR", "broken_start", start_id, "(StartAction)",
            f"StartAction references block {_short(start_id)!r} which does not exist",
        ))

    all_targets: set = set()
    if start_id:
        all_targets.add(start_id)

    for action in actions.values():
        trans = action.get("Transitions") or {}
        nxt   = trans.get("NextAction")
        if nxt:
            all_targets.add(nxt)
        for e in trans.get("Errors") or []:
            if e.get("NextAction"):
                all_targets.add(e["NextAction"])
        for c in trans.get("Conditions") or []:
            if c.get("NextAction"):
                all_targets.add(c["NextAction"])

    for aid, action in actions.items():
        atype  = action.get("Type", "Unknown")
        trans  = action.get("Transitions") or {}
        params = action.get("Parameters") or {}
        errors = trans.get("Errors") or []
        conds  = trans.get("Conditions") or []
        nxt    = trans.get("NextAction") or ""

        if nxt and nxt not in actions:
            issues.append(Issue("ERROR", "broken_target", aid, atype,
                                f"Default branch → {_short(nxt)!r} not found in flow"))
        for e in errors:
            dst = e.get("NextAction") or ""
            if dst and dst not in actions:
                issues.append(Issue("ERROR", "broken_target", aid, atype,
                                    f"Error branch ({e.get('ErrorType', '?')}) → {_short(dst)!r} not found"))
        for c in conds:
            dst = c.get("NextAction") or ""
            if dst and dst not in actions:
                issues.append(Issue("ERROR", "broken_target", aid, atype,
                                    f"Condition branch → {_short(dst)!r} not found in flow"))

        if atype not in TERMINAL_TYPES:
            has_out = bool(nxt) or any(e.get("NextAction") for e in errors) or \
                      any(c.get("NextAction") for c in conds)
            if not has_out:
                issues.append(Issue("ERROR", "dead_end", aid, atype,
                                    "No outgoing transitions — contact will get stuck here"))

        if atype in ERROR_CAPABLE_TYPES:
            if not any(e.get("NextAction") for e in errors):
                issues.append(Issue("WARN", "missing_error_branch", aid, atype,
                                    "No error handler — a failure will leave the contact with no path forward"))

        if atype in DECISION_TYPES and conds and not nxt:
            issues.append(Issue("WARN", "missing_default", aid, atype,
                                "Has condition branches but no default (fallback) branch"))

        if aid not in all_targets and aid != start_id:
            issues.append(Issue("WARN", "unreachable", aid, atype,
                                "Never referenced by any other block — this block is dead code"))

        if atype == "InvokeLambdaFunction":
            if not (params.get("LambdaFunctionARN") or "").strip():
                issues.append(Issue("ERROR", "missing_lambda_arn", aid, atype,
                                    "LambdaFunctionARN is empty"))

        if atype == "SetQueue":
            qid = (params.get("Queue") or {}).get("Id") or params.get("QueueId") or ""
            if not qid.strip():
                issues.append(Issue("WARN", "missing_queue", aid, atype,
                                    "No queue configured"))

    return issues


# ═══════════════════════════════════════════════════════════════════════════════
# OPTIMIZE — rule-based suggestions
# ═══════════════════════════════════════════════════════════════════════════════

_LARGE_FLOW_THRESHOLD  = 40
_MAX_MENU_OPTIONS      = 5
_MIN_DUPLICATE_TEXT_LEN = 10
_DUPLICATE_MIN_COUNT   = 3


class Suggestion(NamedTuple):
    level:      str   # WARN | SUGGEST
    category:   str   # ux | reliability | structure | maintainability
    block_id:   str
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


def analyse_flow(content: dict, flow_type: str = "") -> list:
    suggestions: list = []
    raw     = content.get("Actions") or []
    actions = {a["Identifier"]: a for a in raw if "Identifier" in a}

    if not actions:
        return suggestions

    by_type:    dict = defaultdict(list)
    successors: dict = {}

    for aid, action in actions.items():
        atype = action.get("Type", "")
        by_type[atype].append(aid)
        nxt = (action.get("Transitions") or {}).get("NextAction") or ""
        if nxt:
            successors[aid] = nxt

    for aid in by_type.get("GetUserInput", []):
        action = actions[aid]
        trans  = action.get("Transitions") or {}
        conds  = trans.get("Conditions") or []
        errors = trans.get("Errors") or []
        if len(conds) > _MAX_MENU_OPTIONS:
            suggestions.append(Suggestion(
                "WARN", "ux", aid, "GetUserInput",
                f"{len(conds)} menu options — consider reducing to {_MAX_MENU_OPTIONS} or fewer; "
                "callers struggle with long option lists",
            ))
        if not any(e.get("NextAction") for e in errors):
            suggestions.append(Suggestion(
                "WARN", "ux", aid, "GetUserInput",
                "No error handler — callers who press an invalid key or time out have no path forward",
            ))

    if by_type.get("TransferContactToQueue") and not by_type.get("CheckStaffingStatus"):
        suggestions.append(Suggestion(
            "SUGGEST", "reliability", "", "",
            "Flow transfers to a queue but never checks staffing — callers may be transferred "
            "to a queue with no agents available",
        ))

    if flow_type in ("CONTACT_FLOW", ""):
        if (by_type.get("TransferContactToQueue") or by_type.get("SetQueue")) and \
                not by_type.get("CheckHoursOfOperation"):
            suggestions.append(Suggestion(
                "SUGGEST", "reliability", "", "",
                "No hours-of-operation check — callers may be routed to a queue outside business hours",
            ))

    if len(actions) > _LARGE_FLOW_THRESHOLD:
        suggestions.append(Suggestion(
            "SUGGEST", "structure", "", "",
            f"Flow has {len(actions)} blocks (>{_LARGE_FLOW_THRESHOLD}) — "
            "consider splitting into sub-flows via InvokeFlowModule for easier maintenance",
        ))

    lambda_ids = set(by_type.get("InvokeLambdaFunction", []))
    for aid in lambda_ids:
        nxt = successors.get(aid, "")
        if nxt and nxt in lambda_ids:
            fn1 = (actions[aid].get("Parameters") or {}).get("LambdaFunctionARN", "")
            fn2 = (actions[nxt].get("Parameters") or {}).get("LambdaFunctionARN", "")
            suggestions.append(Suggestion(
                "SUGGEST", "structure", aid, "InvokeLambdaFunction",
                f"Back-to-back Lambda calls: {fn1.split(':')[-1]!r} → {fn2.split(':')[-1]!r} — "
                "consider combining into a single function to reduce latency",
            ))

    text_sources: dict = defaultdict(list)
    for aid, action in actions.items():
        text = (action.get("Parameters") or {}).get("Text") or ""
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


# ═══════════════════════════════════════════════════════════════════════════════
# Shared AWS helpers
# ═══════════════════════════════════════════════════════════════════════════════

def make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
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


def describe_flow_content(client, instance_id, flow_id):
    try:
        raw     = client.describe_contact_flow(InstanceId=instance_id, ContactFlowId=flow_id)
        content = raw["ContactFlow"].get("Content") or ""
        return json.loads(content) if isinstance(content, str) else content
    except (ClientError, json.JSONDecodeError):
        return None


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
        print("Error: file does not look like a contact flow (no 'Actions' array).", file=sys.stderr)
        sys.exit(1)
    return name, ftype, content


# ═══════════════════════════════════════════════════════════════════════════════
# Output
# ═══════════════════════════════════════════════════════════════════════════════

def _hr(width=72):
    print("  " + "─" * width)


def _issue_kind_label(kind: str) -> str:
    return {
        "broken_start":         "broken start action",
        "broken_target":        "broken block reference",
        "dead_end":             "dead-end block",
        "missing_error_branch": "missing error handler",
        "missing_default":      "missing default branch",
        "unreachable":          "unreachable block",
        "missing_lambda_arn":   "missing Lambda ARN",
        "missing_queue":        "missing queue",
    }.get(kind, kind)


def _severity_fmt(sev: str) -> str:
    if sev == "ERROR":
        return "\033[31m[ERROR]\033[0m"
    return "\033[33m[WARN ]\033[0m"


def _wrap(text: str, width: int = 66, indent: str = "             ") -> str:
    words, line, lines = text.split(), [], []
    for w in words:
        if sum(len(x) + 1 for x in line) + len(w) > width:
            lines.append(" ".join(line))
            line = [w]
        else:
            line.append(w)
    if line:
        lines.append(" ".join(line))
    return ("\n" + indent).join(lines)


def print_flow_result(flow_name: str, n_blocks: int,
                      issues: list, suggestions: list,
                      do_scan: bool, do_optimize: bool):
    n_errors  = sum(1 for i in issues if i.severity == "ERROR")
    n_warns   = sum(1 for i in issues if i.severity == "WARN")
    n_warn_s  = sum(1 for s in suggestions if s.level == "WARN")
    n_suggest = sum(1 for s in suggestions if s.level == "SUGGEST")

    _hr()
    print(f"  FLOW ANALYZE   {flow_name}")
    _hr()

    # Summary line
    parts = [f"{n_blocks} block(s)"]
    if do_scan:
        if not issues:
            parts.append("\033[32mscan: ✓ clean\033[0m")
        else:
            p = []
            if n_errors: p.append(f"\033[31m{n_errors} ERROR\033[0m")
            if n_warns:  p.append(f"\033[33m{n_warns} WARN\033[0m")
            parts.append("scan: " + "  ".join(p))
    if do_optimize:
        if not suggestions:
            parts.append("\033[32moptimize: ✓ clean\033[0m")
        else:
            p = []
            if n_warn_s:  p.append(f"\033[33m{n_warn_s} WARN\033[0m")
            if n_suggest: p.append(f"\033[36m{n_suggest} SUGGEST\033[0m")
            parts.append("optimize: " + "  ".join(p))
    print(f"  {'   |   '.join(parts)}")

    # ── Scan findings ─────────────────────────────────────────────────────────
    if do_scan and issues:
        print()
        print("  ERRORS & WARNINGS")
        print(f"  {'─' * 68}")
        by_block: dict = {}
        for iss in issues:
            by_block.setdefault((iss.block_id, iss.block_type), []).append(iss)
        for (bid, btype), block_issues in by_block.items():
            label = f'"{_short(bid)}"  ({btype})' if btype else f'"{_short(bid)}"'
            print(f"  {label}")
            for iss in block_issues:
                print(f"    {_severity_fmt(iss.severity)}  {_issue_kind_label(iss.kind)}")
                print(f"             {iss.detail}")
            print()

    # ── Optimize findings ─────────────────────────────────────────────────────
    if do_optimize and suggestions:
        print()
        print("  SUGGESTIONS")
        by_cat: dict = defaultdict(list)
        for s in suggestions:
            by_cat[s.category].append(s)
        for cat in ("ux", "reliability", "structure", "maintainability"):
            items = by_cat.get(cat, [])
            if not items:
                continue
            print(f"\n  {_CAT_LABELS[cat]}")
            print(f"  {'─' * 68}")
            for s in items:
                loc = f'  "{s.block_id}"' if s.block_id else "  (flow level)"
                print(f"  {_LEVEL_COLOURS[s.level]}  {loc}")
                print(f"             {_wrap(s.detail)}")
        print()

    _hr()
    print()


def print_bulk_summary(results: list, do_scan: bool, do_optimize: bool):
    total = len(results)
    _hr()
    print(f"  FLOW ANALYZE — BULK   ({total} flows)")
    _hr()

    name_w = min(max((len(n) for n, *_ in results), default=10), 50)

    for row in sorted(results, key=lambda r: r[0].lower()):
        flow_name, n_blocks, issues, suggestions = row
        name_col = flow_name[:name_w]
        parts = [f"{n_blocks:>4} blk"]

        if do_scan:
            n_err = sum(1 for i in issues if i.severity == "ERROR")
            n_wrn = sum(1 for i in issues if i.severity == "WARN")
            if not issues:
                parts.append("\033[32m✓ scan\033[0m")
            else:
                s = []
                if n_err: s.append(f"\033[31m{n_err}E\033[0m")
                if n_wrn: s.append(f"\033[33m{n_wrn}W\033[0m")
                parts.append("scan:" + "/".join(s))

        if do_optimize:
            n_w = sum(1 for s in suggestions if s.level == "WARN")
            n_s = sum(1 for s in suggestions if s.level == "SUGGEST")
            if not suggestions:
                parts.append("\033[32m✓ opt\033[0m")
            else:
                s = []
                if n_w: s.append(f"\033[33m{n_w}W\033[0m")
                if n_s: s.append(f"\033[36m{n_s}S\033[0m")
                parts.append("opt:" + "/".join(s))

        print(f"  {name_col:<{name_w}}   {'   '.join(parts)}")

    _hr()
    clean_scan = sum(1 for _, _, iss, _ in results if not iss) if do_scan else 0
    clean_opt  = sum(1 for _, _, _, sug in results if not sug) if do_optimize else 0

    if do_scan:
        total_issues = sum(len(iss) for _, _, iss, _ in results)
        print(f"  Scan: {total_issues} issue(s) across "
              f"{total - clean_scan}/{total} flow(s).", end="  ")
    if do_optimize:
        total_sug = sum(len(sug) for _, _, _, sug in results)
        print(f"  Optimize: {total_sug} suggestion(s) across "
              f"{total - clean_opt}/{total} flow(s).", end="")
    print()
    if do_scan and any(iss for _, _, iss, _ in results):
        print("  Run with --detail to see per-block breakdown.")
    print()


def print_bulk_detail(results: list, do_scan: bool, do_optimize: bool):
    for flow_name, n_blocks, issues, suggestions in sorted(results, key=lambda r: r[0].lower()):
        if issues or suggestions:
            print_flow_result(flow_name, n_blocks, issues, suggestions, do_scan, do_optimize)


# ═══════════════════════════════════════════════════════════════════════════════
# JSON serialisation
# ═══════════════════════════════════════════════════════════════════════════════

def _to_json(flow_name: str, n_blocks: int,
             issues: list | None, suggestions: list | None) -> dict:
    doc: dict = {"flow": flow_name, "block_count": n_blocks}
    if issues is not None:
        doc["scan"] = {
            "issue_count": len(issues),
            "errors":      sum(1 for i in issues if i.severity == "ERROR"),
            "warnings":    sum(1 for i in issues if i.severity == "WARN"),
            "issues": [
                {"severity": i.severity, "kind": i.kind,
                 "block_id": i.block_id, "block_type": i.block_type, "detail": i.detail}
                for i in issues
            ],
        }
    if suggestions is not None:
        doc["optimize"] = {
            "suggestion_count": len(suggestions),
            "warns":            sum(1 for s in suggestions if s.level == "WARN"),
            "suggestions": [
                {"level": s.level, "category": s.category,
                 "block_id": s.block_id, "block_type": s.block_type, "detail": s.detail}
                for s in suggestions
            ],
        }
    return doc


def _bulk_to_json(results: list) -> dict:
    flows = [_to_json(n, b, iss, sug) for n, b, iss, sug in results]
    out: dict = {"flow_count": len(flows), "flows": flows}
    if any("scan" in f for f in flows):
        out["total_issues"]      = sum(f.get("scan", {}).get("issue_count", 0) for f in flows)
        out["flows_with_issues"] = sum(1 for f in flows if f.get("scan", {}).get("issue_count", 0))
    if any("optimize" in f for f in flows):
        out["total_suggestions"]      = sum(f.get("optimize", {}).get("suggestion_count", 0) for f in flows)
        out["flows_with_suggestions"] = sum(1 for f in flows if f.get("optimize", {}).get("suggestion_count", 0))
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# CSV output
# ═══════════════════════════════════════════════════════════════════════════════

def write_csv(results: list, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["flow_name", "block_count", "issue_count",
                    "severity", "kind", "block_id", "block_type", "detail"])
        for flow_name, n_blocks, issues, _ in results:
            if issues:
                for iss in issues:
                    w.writerow([flow_name, n_blocks, len(issues),
                                iss.severity, iss.kind,
                                iss.block_id, iss.block_type, iss.detail])
            else:
                w.writerow([flow_name, n_blocks, 0, "", "", "", "", ""])
    print(f"  CSV written → {path}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════════════
# Argument parsing
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="Scan and optimize Amazon Connect contact flows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s Main_IVR.json
  %(prog)s --instance-id <UUID> --name "Main IVR" --scan
  %(prog)s --instance-id <UUID> --all --detail
  %(prog)s --instance-id <UUID> --all --json | jq '.flows[] | select(.scan.issue_count > 0)'
        """,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("flow_file", nargs="?", metavar="FLOW_JSON",
                     help="Local exported flow JSON file")
    src.add_argument("--all", action="store_true", help="Analyse all flows in the instance")

    p.add_argument("--instance-id", default=None, metavar="UUID")
    p.add_argument("--name",    default=None, metavar="NAME",
                   help="Flow name to analyse (case-insensitive substring)")
    p.add_argument("--type",    default=None, metavar="TYPE",
                   help="Filter by flow type with --all (e.g. CONTACT_FLOW)")
    p.add_argument("--region",  default=None)
    p.add_argument("--profile", default=None)

    mode = p.add_argument_group("analysis mode (default: both)")
    mode.add_argument("--scan",     action="store_true", help="Error scanner only")
    mode.add_argument("--optimize", action="store_true", help="Optimization suggestions only")

    p.add_argument("--detail", action="store_true",
                   help="Per-block breakdown in bulk mode")
    p.add_argument("--csv",  default=None, metavar="FILE",
                   help="Write scan issues to CSV")
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Emit raw JSON")

    args = p.parse_args()

    if not args.flow_file and not args.all and not args.name:
        p.error("provide a FLOW_JSON file, --name <name>, or --all")
    if (args.all or args.name) and not args.instance_id:
        p.error("--instance-id is required with --name and --all")
    if args.flow_file and args.instance_id:
        p.error("--instance-id cannot be combined with a local FLOW_JSON file")

    # Default: run both passes
    if not args.scan and not args.optimize:
        args.scan = args.optimize = True

    return args


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args = parse_args()

    # Load snapshot for name resolution
    global _snapshot
    if args.instance_id and _ct_snapshot:
        _snapshot = _ct_snapshot.load(args.instance_id)
        if _snapshot:
            _ct_snapshot.warn_if_stale(_snapshot)

    def _run(name, ftype, content):
        n_blocks    = len(content.get("Actions") or [])
        issues      = scan_flow(content)      if args.scan     else None
        suggestions = analyse_flow(content, ftype) if args.optimize else None
        return name, n_blocks, issues or [], suggestions or []

    # ── Local file ────────────────────────────────────────────────────────────
    if args.flow_file:
        name, ftype, content = load_content_from_file(args.flow_file)
        name, n_blocks, issues, suggestions = _run(name, ftype, content)
        issues_arg      = issues      if args.scan     else None
        suggestions_arg = suggestions if args.optimize else None
        if args.output_json:
            print(json.dumps(_to_json(name, n_blocks, issues_arg, suggestions_arg), indent=2))
        else:
            print_flow_result(name, n_blocks, issues, suggestions, args.scan, args.optimize)
        if args.csv:
            write_csv([(name, n_blocks, issues, suggestions)], args.csv)
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

        ftype = summary.get("ContactFlowType", "")
        name, n_blocks, issues, suggestions = _run(summary["Name"], ftype, content)
        issues_arg      = issues      if args.scan     else None
        suggestions_arg = suggestions if args.optimize else None
        if args.output_json:
            print(json.dumps(_to_json(name, n_blocks, issues_arg, suggestions_arg), indent=2))
        else:
            print_flow_result(name, n_blocks, issues, suggestions, args.scan, args.optimize)
        if args.csv:
            write_csv([(name, n_blocks, issues, suggestions)], args.csv)
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
        ftype = summary.get("ContactFlowType", "")
        _, n_blocks, issues, suggestions = _run(summary["Name"], ftype, content)
        results.append((summary["Name"], n_blocks, issues, suggestions))

    issues_fn      = (lambda r: r[2]) if args.scan     else (lambda r: None)
    suggestions_fn = (lambda r: r[3]) if args.optimize else (lambda r: None)

    if args.output_json:
        doc_results = [(n, b, issues_fn(r), suggestions_fn(r))
                       for r, (n, b, _, __) in zip(results, results)]
        print(json.dumps(_bulk_to_json(
            [(n, b, issues_fn((n, b, iss, sug)), suggestions_fn((n, b, iss, sug)))
             for n, b, iss, sug in results]
        ), indent=2))
    else:
        print_bulk_summary(results, args.scan, args.optimize)
        if args.detail:
            print_bulk_detail(results, args.scan, args.optimize)
    if args.csv:
        write_csv(results, args.csv)


if __name__ == "__main__":
    main()
