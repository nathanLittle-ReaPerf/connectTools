#!/usr/bin/env python3
"""flow_scan.py — Scan Amazon Connect contact flows for configuration issues.

Checks for: broken block references, dead-end blocks, missing error handlers,
missing default branches on decision blocks, unreachable blocks, and empty
Lambda/queue parameters.

Works on local exported JSON files or live instance flows (single or bulk).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import NamedTuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

# ── Block classification ──────────────────────────────────────────────────────

# Blocks that end the flow — no NextAction required
TERMINAL_TYPES = {
    "DisconnectParticipant",
    "TransferContactToQueue",
    "TransferContactToFlow",
    "EndFlowExecution",
}

# Decision blocks — branch on conditions; should have a default (NextAction)
DECISION_TYPES = {
    "CheckAttribute",
    "CheckContactAttributes",
    "GetUserInput",
    "CheckHoursOfOperation",
    "CheckStaffingStatus",
    "CheckAgentStatus",
}

# Blocks that can throw system errors and should have an error handler
ERROR_CAPABLE_TYPES = {
    "InvokeLambdaFunction",
    "InvokeFlowModule",
    "TransferContactToQueue",
}


# ── Issue ─────────────────────────────────────────────────────────────────────

class Issue(NamedTuple):
    severity:   str   # ERROR | WARN
    kind:       str   # issue type key
    block_id:   str   # action Identifier
    block_type: str   # action Type
    detail:     str   # human-readable message


# ── Scanner ───────────────────────────────────────────────────────────────────

def scan_flow(content: dict) -> list:
    """Scan a parsed flow content dict and return a list of Issues."""
    issues  = []
    raw     = content.get("Actions") or []
    actions = {a["Identifier"]: a for a in raw if "Identifier" in a}
    start_id = content.get("StartAction")

    if not actions:
        return issues  # empty flow — nothing to check

    # ── Broken StartAction ────────────────────────────────────────────────────
    if start_id and start_id not in actions:
        issues.append(Issue(
            "ERROR", "broken_start", start_id, "(StartAction)",
            f"StartAction references block {_short(start_id)!r} which does not exist",
        ))

    # ── Build the set of all referenced target IDs ────────────────────────────
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

    # ── Per-block checks ──────────────────────────────────────────────────────
    for aid, action in actions.items():
        atype  = action.get("Type", "Unknown")
        trans  = action.get("Transitions") or {}
        params = action.get("Parameters") or {}
        errors = trans.get("Errors") or []
        conds  = trans.get("Conditions") or []
        nxt    = trans.get("NextAction") or ""

        # 1. Broken target references
        if nxt and nxt not in actions:
            issues.append(Issue(
                "ERROR", "broken_target", aid, atype,
                f"Default branch → {_short(nxt)!r} not found in flow",
            ))
        for e in errors:
            dst = e.get("NextAction") or ""
            if dst and dst not in actions:
                issues.append(Issue(
                    "ERROR", "broken_target", aid, atype,
                    f"Error branch ({e.get('ErrorType', '?')}) → {_short(dst)!r} not found in flow",
                ))
        for c in conds:
            dst = c.get("NextAction") or ""
            if dst and dst not in actions:
                issues.append(Issue(
                    "ERROR", "broken_target", aid, atype,
                    f"Condition branch → {_short(dst)!r} not found in flow",
                ))

        # 2. Dead-end non-terminal block (no outgoing transitions at all)
        if atype not in TERMINAL_TYPES:
            has_out = (
                bool(nxt)
                or any(e.get("NextAction") for e in errors)
                or any(c.get("NextAction") for c in conds)
            )
            if not has_out:
                issues.append(Issue(
                    "ERROR", "dead_end", aid, atype,
                    "No outgoing transitions — contact will get stuck here",
                ))

        # 3. Missing error handler on error-capable blocks
        if atype in ERROR_CAPABLE_TYPES:
            if not any(e.get("NextAction") for e in errors):
                issues.append(Issue(
                    "WARN", "missing_error_branch", aid, atype,
                    "No error handler — a failure will leave the contact with no path forward",
                ))

        # 4. Missing default branch on decision blocks
        if atype in DECISION_TYPES and conds:
            if not nxt:
                issues.append(Issue(
                    "WARN", "missing_default", aid, atype,
                    "Has condition branches but no default (fallback) branch",
                ))

        # 5. Unreachable block
        if aid not in all_targets and aid != start_id:
            issues.append(Issue(
                "WARN", "unreachable", aid, atype,
                "Never referenced by any other block — this block is dead code",
            ))

        # 6. Missing Lambda ARN
        if atype == "InvokeLambdaFunction":
            arn = params.get("LambdaFunctionARN") or ""
            if not arn.strip():
                issues.append(Issue(
                    "ERROR", "missing_lambda_arn", aid, atype,
                    "LambdaFunctionARN is empty",
                ))

        # 7. Missing queue on SetQueue
        if atype == "SetQueue":
            qid = (params.get("Queue") or {}).get("Id") or params.get("QueueId") or ""
            if not qid.strip():
                issues.append(Issue(
                    "WARN", "missing_queue", aid, atype,
                    "No queue configured",
                ))

    return issues


def _short(identifier: str) -> str:
    """Shorten a UUID to first 8 chars; leave human-readable names as-is."""
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}", identifier.lower()):
        return identifier[:8] + "…"
    return identifier[:40]


def _block_label(identifier: str, block_type: str) -> str:
    short = _short(identifier)
    return f'"{short}"  ({block_type})'


# ── Flow content loader ───────────────────────────────────────────────────────

def load_content_from_file(path: str) -> tuple:
    """Load a flow JSON file. Returns (flow_name, content_dict)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if "content" in data and "Actions" in (data.get("content") or {}):
        name    = (data.get("metadata") or {}).get("name") or path
        content = data["content"]
    elif "Actions" in data:
        name    = path
        content = data
    else:
        print("Error: file does not look like a contact flow (no 'Actions' array).", file=sys.stderr)
        sys.exit(1)

    return name, content


# ── AWS helpers ───────────────────────────────────────────────────────────────

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
            code = e.response["Error"]["Code"]
            msg  = e.response["Error"]["Message"]
            print(f"Error listing flows [{code}]: {msg}", file=sys.stderr)
            sys.exit(1)
        flows.extend(resp.get("ContactFlowSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return flows


def describe_flow_content(client, instance_id, flow_id) -> dict | None:
    try:
        raw = client.describe_contact_flow(
            InstanceId=instance_id, ContactFlowId=flow_id
        )["ContactFlow"]
        content = raw.get("Content") or ""
        return json.loads(content) if isinstance(content, str) else content
    except ClientError as e:
        print(f"  Warning: could not describe flow {flow_id}: {e.response['Error']['Message']}",
              file=sys.stderr)
        return None
    except json.JSONDecodeError:
        return None


# ── Human-readable output ─────────────────────────────────────────────────────

def _hr(width=72):
    print("  " + "─" * width)


def _severity_fmt(sev: str) -> str:
    if sev == "ERROR":
        return "\033[31m[ERROR]\033[0m"
    return "\033[33m[WARN ]\033[0m"


def _issue_kind_label(kind: str) -> str:
    return {
        "broken_start":        "broken start action",
        "broken_target":       "broken block reference",
        "dead_end":            "dead-end block",
        "missing_error_branch":"missing error handler",
        "missing_default":     "missing default branch",
        "unreachable":         "unreachable block",
        "missing_lambda_arn":  "missing Lambda ARN",
        "missing_queue":       "missing queue",
    }.get(kind, kind)


def print_flow_result(flow_name: str, n_blocks: int, issues: list):
    """Print results for a single flow."""
    n_errors = sum(1 for i in issues if i.severity == "ERROR")
    n_warns  = sum(1 for i in issues if i.severity == "WARN")

    _hr()
    print(f"  FLOW SCAN   {flow_name}")
    _hr()
    print(f"  {n_blocks} block(s) scanned   ", end="")
    if not issues:
        print("\033[32m✓ No issues found\033[0m")
        return
    print(f"{len(issues)} issue(s) found   "
          f"\033[31m{n_errors} ERROR\033[0m  \033[33m{n_warns} WARN\033[0m")
    print()

    # Group by block
    by_block: dict = {}
    for iss in issues:
        key = (iss.block_id, iss.block_type)
        by_block.setdefault(key, []).append(iss)

    for (bid, btype), block_issues in by_block.items():
        print(f"  {_block_label(bid, btype)}")
        for iss in block_issues:
            kind_str = _issue_kind_label(iss.kind)
            print(f"    {_severity_fmt(iss.severity)}  {kind_str}")
            print(f"             {iss.detail}")
        print()


def print_bulk_summary(results: list):
    """
    results: list of (flow_name, n_blocks, issues)
    """
    total_flows   = len(results)
    flows_with    = sum(1 for _, _, iss in results if iss)
    total_issues  = sum(len(iss) for _, _, iss in results)
    total_errors  = sum(sum(1 for i in iss if i.severity == "ERROR") for _, _, iss in results)
    total_warns   = sum(sum(1 for i in iss if i.severity == "WARN")  for _, _, iss in results)

    _hr()
    print(f"  FLOW SCAN — BULK   ({total_flows} flows)")
    _hr()

    # Column widths
    name_w = max((len(n) for n, _, _ in results), default=10)
    name_w = min(max(name_w, 10), 50)

    for flow_name, n_blocks, issues in sorted(results, key=lambda r: r[0].lower()):
        n_err = sum(1 for i in issues if i.severity == "ERROR")
        n_wrn = sum(1 for i in issues if i.severity == "WARN")
        name_col = flow_name[:name_w]
        if not issues:
            status = "\033[32m✓ clean\033[0m"
        else:
            parts = []
            if n_err:
                parts.append(f"\033[31m{n_err} ERROR\033[0m")
            if n_wrn:
                parts.append(f"\033[33m{n_wrn} WARN\033[0m")
            status = "  ".join(parts)
        print(f"  {name_col:<{name_w}}  {n_blocks:>4} blocks   {status}")

    _hr()
    if total_issues == 0:
        print(f"  \033[32m✓ All {total_flows} flows are clean\033[0m")
    else:
        print(f"  {total_issues} issue(s) across {flows_with}/{total_flows} flow(s)   "
              f"\033[31m{total_errors} ERROR\033[0m  \033[33m{total_warns} WARN\033[0m")
        print(f"  Run with --detail to see per-block breakdown, or --json for full output")
    print()


def print_bulk_detail(results: list):
    """Full per-block breakdown for all flows that have issues."""
    for flow_name, n_blocks, issues in sorted(results, key=lambda r: r[0].lower()):
        if issues:
            print_flow_result(flow_name, n_blocks, issues)


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Scan Amazon Connect contact flows for configuration issues.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Scan a local exported file
  %(prog)s flow.json

  # Scan a single flow by name from a live instance
  %(prog)s --instance-id <UUID> --name "Main IVR" --region us-east-1

  # Scan every flow in the instance
  %(prog)s --instance-id <UUID> --all

  # Scan by flow type with full per-block detail
  %(prog)s --instance-id <UUID> --all --type CONTACT_FLOW --detail

  # JSON output (pipe to jq)
  %(prog)s --instance-id <UUID> --all --json | jq '.flows[] | select(.issue_count > 0)'
        """,
    )

    # Input source (mutually exclusive)
    src = p.add_mutually_exclusive_group()
    src.add_argument("flow_file", nargs="?", metavar="FLOW_JSON",
                     help="Local exported flow JSON file")
    src.add_argument("--all", action="store_true",
                     help="Scan all flows in the instance")

    # Instance options (used with --name or --all)
    p.add_argument("--instance-id", default=None, metavar="UUID")
    p.add_argument("--name",  default=None, metavar="NAME",
                   help="Flow name to scan (case-insensitive substring)")
    p.add_argument("--type",  default=None, metavar="TYPE",
                   help="Filter by flow type when using --all (e.g. CONTACT_FLOW)")
    p.add_argument("--region",  default=None)
    p.add_argument("--profile", default=None)

    # Output
    p.add_argument("--detail", action="store_true",
                   help="Show per-block breakdown in bulk mode")
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Emit raw JSON")

    args = p.parse_args()

    if not args.flow_file and not args.all and not args.name:
        p.error("provide a FLOW_JSON file, --name <name>, or --all")
    if (args.all or args.name) and not args.instance_id:
        p.error("--instance-id is required with --name and --all")

    return args


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    # ── Local file mode ───────────────────────────────────────────────────────
    if args.flow_file:
        flow_name, content = load_content_from_file(args.flow_file)
        n_blocks = len(content.get("Actions") or [])
        issues   = scan_flow(content)

        if args.output_json:
            print(json.dumps(_to_json_single(flow_name, n_blocks, issues), indent=2))
        else:
            print_flow_result(flow_name, n_blocks, issues)
        return

    client = make_client(args.region, args.profile)

    # ── Single flow by name ───────────────────────────────────────────────────
    if args.name and not args.all:
        print(f"  Listing flows...", file=sys.stderr)
        all_flows = list_all_flows(client, args.instance_id)
        name_lower = args.name.lower()
        matches = [f for f in all_flows if name_lower in f["Name"].lower()]

        if not matches:
            print(f"No flows found matching {args.name!r}.", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"Found {len(matches)} flows matching {args.name!r} — use a more specific name:",
                  file=sys.stderr)
            for f in matches:
                print(f"  {f['Name']!r}", file=sys.stderr)
            sys.exit(1)

        summary = matches[0]
        print(f"  Scanning '{summary['Name']}'...", file=sys.stderr)
        content  = describe_flow_content(client, args.instance_id, summary["Id"])
        if content is None:
            print("Error: could not load flow content.", file=sys.stderr)
            sys.exit(1)

        n_blocks = len(content.get("Actions") or [])
        issues   = scan_flow(content)

        if args.output_json:
            print(json.dumps(_to_json_single(summary["Name"], n_blocks, issues), indent=2))
        else:
            print_flow_result(summary["Name"], n_blocks, issues)
        return

    # ── Bulk mode (--all) ─────────────────────────────────────────────────────
    flow_types = [args.type] if args.type else None
    print(f"  Listing flows...", file=sys.stderr)
    all_flows = list_all_flows(client, args.instance_id, flow_types)

    if not all_flows:
        print("No flows found.", file=sys.stderr)
        sys.exit(0)

    print(f"  Scanning {len(all_flows)} flow(s)...", file=sys.stderr)
    results = []
    for i, summary in enumerate(all_flows, 1):
        print(f"  [{i}/{len(all_flows)}] {summary['Name']}", file=sys.stderr)
        content = describe_flow_content(client, args.instance_id, summary["Id"])
        if content is None:
            continue
        n_blocks = len(content.get("Actions") or [])
        issues   = scan_flow(content)
        results.append((summary["Name"], n_blocks, issues))

    if args.output_json:
        print(json.dumps(_to_json_bulk(results), indent=2))
    else:
        print_bulk_summary(results)
        if args.detail:
            print_bulk_detail(results)


# ── JSON serialisation ────────────────────────────────────────────────────────

def _issue_to_dict(iss: Issue) -> dict:
    return {
        "severity":   iss.severity,
        "kind":       iss.kind,
        "block_id":   iss.block_id,
        "block_type": iss.block_type,
        "detail":     iss.detail,
    }


def _to_json_single(flow_name: str, n_blocks: int, issues: list) -> dict:
    return {
        "flow":        flow_name,
        "block_count": n_blocks,
        "issue_count": len(issues),
        "errors":      sum(1 for i in issues if i.severity == "ERROR"),
        "warnings":    sum(1 for i in issues if i.severity == "WARN"),
        "issues":      [_issue_to_dict(i) for i in issues],
    }


def _to_json_bulk(results: list) -> dict:
    flows = [_to_json_single(name, n, iss) for name, n, iss in results]
    return {
        "flow_count":   len(flows),
        "flows_with_issues": sum(1 for f in flows if f["issue_count"] > 0),
        "total_issues": sum(f["issue_count"] for f in flows),
        "total_errors": sum(f["errors"] for f in flows),
        "total_warnings": sum(f["warnings"] for f in flows),
        "flows": flows,
    }


if __name__ == "__main__":
    main()
