#!/usr/bin/env python3
"""flow_attr_search.py — Search Amazon Connect contact flows for attribute usage.

Find every place a contact attribute is SET, CHECKed, or REFerenced across
one or all flows in an instance, or in a local exported flow JSON file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

_MAN = """\
NAME
    flow_attr_search.py — Search Amazon Connect contact flows for attribute usage

SYNOPSIS
    python flow_attr_search.py --attribute NAME FLOW_JSON [...]
    python flow_attr_search.py --attribute NAME --instance-id UUID --name NAME
    python flow_attr_search.py --attribute NAME --instance-id UUID --all

DESCRIPTION
    Finds every block in a contact flow that:
      SET   — assigns the attribute via UpdateContactAttributes
      CHECK — compares the attribute via a Compare block
      REF   — references the attribute value ($.Attributes.NAME) anywhere
              else in block parameters (Lambda inputs, prompt text, etc.)

    Accepts local exported JSON files (from export_flow.py), a single live
    flow by name, or all flows in an instance at once.

OPTIONS
    --attribute NAME
        Attribute key to search for. Case-insensitive by default.

    FLOW_JSON [...]
        One or more local exported flow JSON files to search.

    --instance-id UUID
        Amazon Connect instance UUID. Required with --name or --all.

    --name NAME
        Flow name to search (case-insensitive substring). Requires --instance-id.

    --all
        Search all flows in the instance. Requires --instance-id.

    --type TYPE
        Filter flow type when using --all (e.g. CONTACT_FLOW).

    --exact
        Match attribute name with exact case (default is case-insensitive).

    --detail
        Show per-block detail in bulk (--all) mode. Without this flag only
        a summary table is shown.

    --json
        Emit raw JSON output.

    --region REGION
        AWS region. Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

EXAMPLES
    # Search a local file
    python flow_attr_search.py --attribute rP_CallReceived S360_Home.json

    # Search a single live flow
    python flow_attr_search.py --attribute authToken --instance-id <UUID> --name "Main IVR"

    # Search every flow in the instance
    python flow_attr_search.py --attribute authToken --instance-id <UUID> --all

    # Bulk search with per-block detail, JSON output
    python flow_attr_search.py --attribute authToken --instance-id <UUID> --all --detail
    python flow_attr_search.py --attribute authToken --instance-id <UUID> --all --json | jq '.flows[] | select(.hit_count > 0)'

HIT KINDS
    SET   — attribute key set in UpdateContactAttributes
    CHECK — attribute value compared in a Compare block
    REF   — attribute value ($.Attributes.NAME) read in any other context

IAM PERMISSIONS
    connect:ListContactFlows
    connect:DescribeContactFlow
"""


# ── Hit ───────────────────────────────────────────────────────────────────────

class Hit(NamedTuple):
    kind:       str   # SET | CHECK | REF
    block_id:   str
    block_type: str
    detail:     str


# ── Search logic ──────────────────────────────────────────────────────────────

def _ref_pattern(attr_name: str, exact: bool) -> re.Pattern:
    """Matches $.Attributes.<name> as a whole token (word-boundary after name)."""
    flags = 0 if exact else re.IGNORECASE
    return re.compile(
        r'\$\.Attributes\.' + re.escape(attr_name) + r'(?=[^a-zA-Z0-9_]|$)',
        flags,
    )


def _scan_refs(value, pattern: re.Pattern, path: str) -> list[tuple[str, str]]:
    """Recursively find all $.Attributes.NAME occurrences. Returns (path, matched) pairs."""
    results: list[tuple[str, str]] = []
    if isinstance(value, str):
        for m in pattern.finditer(value):
            results.append((path, m.group(0)))
    elif isinstance(value, dict):
        for k, v in value.items():
            results.extend(_scan_refs(v, pattern, f"{path}.{k}"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            results.extend(_scan_refs(v, pattern, f"{path}[{i}]"))
    return results


def _key_matches(key: str, attr_name: str, exact: bool) -> bool:
    return key == attr_name if exact else key.lower() == attr_name.lower()


def search_attribute(content: dict, attr_name: str, exact: bool = False) -> list[Hit]:
    """Return all Hit records for attr_name in a parsed flow content dict."""
    hits: list[Hit] = []
    pattern = _ref_pattern(attr_name, exact)

    for action in content.get("Actions") or []:
        block_id   = action.get("Identifier", "?")
        block_type = action.get("Type", "?")
        params     = action.get("Parameters") or {}
        block_hits: list[Hit] = []

        # ── SET: UpdateContactAttributes keys ─────────────────────────────────
        if block_type == "UpdateContactAttributes":
            attrs_dict = params.get("Attributes") or {}
            for key, val in attrs_dict.items():
                if _key_matches(key, attr_name, exact):
                    block_hits.append(Hit(
                        "SET", block_id, block_type,
                        f"Sets '{key}' = {_fmt_val(val)}",
                    ))
                # Also scan the value — could read the searched attr while setting another
                for ref_path, ref_str in _scan_refs(val, pattern, f"Attributes['{key}'] value"):
                    block_hits.append(Hit(
                        "REF", block_id, block_type,
                        f"{ref_path}: {ref_str!r}",
                    ))
            _append_unique(hits, block_hits)
            continue  # skip general scan; already handled the whole Attributes dict

        # ── CHECK: Compare blocks ──────────────────────────────────────────────
        if block_type == "Compare":
            cmp_val = params.get("ComparisonValue") or ""
            if pattern.search(str(cmp_val)):
                conditions = (action.get("Transitions") or {}).get("Conditions") or []
                operands = [
                    op
                    for c in conditions
                    for op in ((c.get("Condition") or {}).get("Operands") or [])
                ]
                cond_str = ", ".join(repr(str(v)) for v in operands) if operands else "(no conditions)"
                block_hits.append(Hit(
                    "CHECK", block_id, block_type,
                    f"Compares '{cmp_val}' against {cond_str}",
                ))
                _append_unique(hits, block_hits)
                continue  # ComparisonValue handled; don't re-emit as REF

        # ── REF: scan all Parameters for $.Attributes.name ────────────────────
        ref_results = _scan_refs(params, pattern, "Parameters")
        seen_paths: set[str] = set()
        for ref_path, ref_str in ref_results:
            key = f"{ref_path}:{ref_str}"
            if key not in seen_paths:
                seen_paths.add(key)
                block_hits.append(Hit(
                    "REF", block_id, block_type,
                    f"{ref_path}: {ref_str!r}",
                ))
        _append_unique(hits, block_hits)

    return hits


def _fmt_val(val) -> str:
    if isinstance(val, str) and len(val) > 60:
        return repr(val[:57] + "…")
    return repr(val)


def _append_unique(target: list, additions: list) -> None:
    seen = set(target)
    for h in additions:
        if h not in seen:
            seen.add(h)
            target.append(h)


# ── Flow content loader ───────────────────────────────────────────────────────

def load_content_from_file(path: str) -> tuple:
    """Returns (flow_name, content_dict)."""
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

_KIND_COLOR = {
    "SET":   "\033[32m",   # green
    "CHECK": "\033[33m",   # yellow
    "REF":   "\033[36m",   # cyan
}
_RESET = "\033[0m"
_DIM   = "\033[2m"
_BOLD  = "\033[1m"


def _hr(width=72):
    print("  " + "─" * width)


def _short(identifier: str) -> str:
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}", identifier.lower()):
        return identifier[:8] + "…"
    return identifier[:40]


def _kind_badge(kind: str) -> str:
    color = _KIND_COLOR.get(kind, "")
    return f"{color}[{kind:<5}]{_RESET}"


def print_flow_result(flow_name: str, attr_name: str, hits: list[Hit], show_header: bool = True):
    """Print search results for a single flow."""
    if show_header:
        _hr()
        print(f"  {_BOLD}ATTRIBUTE SEARCH{_RESET}   {attr_name!r}   in {flow_name}")
        _hr()

    if not hits:
        print(f"  {_DIM}No hits{_RESET}")
        return

    n_set   = sum(1 for h in hits if h.kind == "SET")
    n_check = sum(1 for h in hits if h.kind == "CHECK")
    n_ref   = sum(1 for h in hits if h.kind == "REF")
    n_blocks = len({h.block_id for h in hits})

    print(f"  {len(hits)} hit(s) across {n_blocks} block(s)"
          f"   {_KIND_COLOR['SET']}SET {n_set}{_RESET}"
          f"  {_KIND_COLOR['CHECK']}CHECK {n_check}{_RESET}"
          f"  {_KIND_COLOR['REF']}REF {n_ref}{_RESET}")
    print()

    # Group by block
    by_block: dict = {}
    for h in hits:
        by_block.setdefault((h.block_id, h.block_type), []).append(h)

    for (bid, btype), block_hits in by_block.items():
        label = _short(bid)
        print(f"  {_BOLD}\"{label}\"{_RESET}  ({btype})")
        for h in block_hits:
            print(f"    {_kind_badge(h.kind)}  {h.detail}")
        print()


def print_bulk_summary(results: list, attr_name: str):
    """results: list of (flow_name, hits)"""
    total_flows    = len(results)
    flows_with_hits = sum(1 for _, hits in results if hits)
    total_hits     = sum(len(hits) for _, hits in results)

    _hr()
    print(f"  {_BOLD}ATTRIBUTE SEARCH{_RESET}   {attr_name!r}   ({total_flows} flows scanned)")
    _hr()

    name_w = max((len(n) for n, _ in results), default=10)
    name_w = min(max(name_w, 10), 50)

    for flow_name, hits in sorted(results, key=lambda r: r[0].lower()):
        n_set   = sum(1 for h in hits if h.kind == "SET")
        n_check = sum(1 for h in hits if h.kind == "CHECK")
        n_ref   = sum(1 for h in hits if h.kind == "REF")
        name_col = flow_name[:name_w]
        if not hits:
            status = f"{_DIM}no hits{_RESET}"
        else:
            parts = []
            if n_set:
                parts.append(f"{_KIND_COLOR['SET']}{n_set} SET{_RESET}")
            if n_check:
                parts.append(f"{_KIND_COLOR['CHECK']}{n_check} CHECK{_RESET}")
            if n_ref:
                parts.append(f"{_KIND_COLOR['REF']}{n_ref} REF{_RESET}")
            status = "  ".join(parts)
        print(f"  {name_col:<{name_w}}  {status}")

    _hr()
    if flows_with_hits == 0:
        print(f"  {_DIM}Not found in any flow{_RESET}")
    else:
        print(f"  Found {total_hits} hit(s) in {flows_with_hits}/{total_flows} flow(s)")
        print(f"  Run with --detail to see per-block breakdown, or --json for full output")
    print()


def print_bulk_detail(results: list, attr_name: str):
    """Per-block detail for all flows with hits."""
    flows_with = [(name, hits) for name, hits in results if hits]
    for flow_name, hits in sorted(flows_with, key=lambda r: r[0].lower()):
        _hr()
        print(f"  {_BOLD}{flow_name}{_RESET}")
        print_flow_result(flow_name, attr_name, hits, show_header=False)


# ── JSON serialisation ────────────────────────────────────────────────────────

def _hit_to_dict(h: Hit) -> dict:
    return {"kind": h.kind, "block_id": h.block_id, "block_type": h.block_type, "detail": h.detail}


def _to_json_single(flow_name: str, attr_name: str, hits: list[Hit]) -> dict:
    return {
        "flow":      flow_name,
        "attribute": attr_name,
        "hit_count": len(hits),
        "set_count":   sum(1 for h in hits if h.kind == "SET"),
        "check_count": sum(1 for h in hits if h.kind == "CHECK"),
        "ref_count":   sum(1 for h in hits if h.kind == "REF"),
        "hits":      [_hit_to_dict(h) for h in hits],
    }


def _to_json_bulk(results: list, attr_name: str) -> dict:
    flows = [_to_json_single(name, attr_name, hits) for name, hits in results]
    return {
        "attribute":        attr_name,
        "flow_count":       len(flows),
        "flows_with_hits":  sum(1 for f in flows if f["hit_count"] > 0),
        "total_hits":       sum(f["hit_count"] for f in flows),
        "flows":            flows,
    }


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Search Amazon Connect contact flows for attribute usage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Search a local file
  %(prog)s --attribute rP_CallReceived S360_Home.json

  # Search a single live flow
  %(prog)s --attribute authToken --instance-id <UUID> --name "Main IVR"

  # Search every flow in the instance with per-block detail
  %(prog)s --attribute authToken --instance-id <UUID> --all --detail

  # JSON output
  %(prog)s --attribute authToken --instance-id <UUID> --all --json | jq '.flows[] | select(.hit_count > 0)'
        """,
    )

    p.add_argument("--attribute", required=True, metavar="NAME",
                   help="Attribute key to search for")

    # Input source
    p.add_argument("flow_files", nargs="*", metavar="FLOW_JSON",
                   help="Local exported flow JSON file(s)")
    p.add_argument("--all", action="store_true",
                   help="Search all flows in the instance")

    p.add_argument("--instance-id", default=None, metavar="UUID")
    p.add_argument("--name",   default=None, metavar="NAME",
                   help="Flow name to search (case-insensitive substring)")
    p.add_argument("--type",   default=None, metavar="TYPE",
                   help="Filter flow type when using --all")
    p.add_argument("--region", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--exact", action="store_true",
                   help="Match attribute name with exact case")
    p.add_argument("--detail", action="store_true",
                   help="Show per-block breakdown in bulk mode")
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Emit raw JSON")

    args = p.parse_args()

    if not args.flow_files and not args.all and not args.name:
        p.error("provide FLOW_JSON file(s), --name <name>, or --all")
    if (args.all or args.name) and not args.instance_id:
        p.error("--instance-id is required with --name and --all")
    if args.flow_files and args.instance_id:
        p.error("--instance-id cannot be combined with local FLOW_JSON files")

    return args


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args = parse_args()
    attr = args.attribute

    # ── Local file mode ───────────────────────────────────────────────────────
    if args.flow_files:
        results = []
        for path in args.flow_files:
            flow_name, content = load_content_from_file(path)
            hits = search_attribute(content, attr, args.exact)
            results.append((flow_name, hits))

        if args.output_json:
            if len(results) == 1:
                flow_name, hits = results[0]
                print(json.dumps(_to_json_single(flow_name, attr, hits), indent=2))
            else:
                print(json.dumps(_to_json_bulk(results, attr), indent=2))
        elif len(results) == 1:
            flow_name, hits = results[0]
            print_flow_result(flow_name, attr, hits)
        else:
            print_bulk_summary(results, attr)
            if args.detail:
                print_bulk_detail(results, attr)
        return

    client = make_client(args.region, args.profile)

    # ── Single flow by name ───────────────────────────────────────────────────
    if args.name and not args.all:
        print(f"  Listing flows…", file=sys.stderr)
        all_flows  = list_all_flows(client, args.instance_id)
        name_lower = args.name.lower()
        matches    = [f for f in all_flows if name_lower in f["Name"].lower()]

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
        print(f"  Searching '{summary['Name']}'…", file=sys.stderr)
        content = describe_flow_content(client, args.instance_id, summary["Id"])
        if content is None:
            print("Error: could not load flow content.", file=sys.stderr)
            sys.exit(1)

        hits = search_attribute(content, attr, args.exact)

        if args.output_json:
            print(json.dumps(_to_json_single(summary["Name"], attr, hits), indent=2))
        else:
            print_flow_result(summary["Name"], attr, hits)
        return

    # ── Bulk mode (--all) ─────────────────────────────────────────────────────
    flow_types = [args.type] if args.type else None
    print(f"  Listing flows…", file=sys.stderr)
    all_flows = list_all_flows(client, args.instance_id, flow_types)

    if not all_flows:
        print("No flows found.", file=sys.stderr)
        sys.exit(0)

    print(f"  Searching {len(all_flows)} flow(s) for {attr!r}…", file=sys.stderr)
    results = []
    for i, summary in enumerate(all_flows, 1):
        print(f"  [{i}/{len(all_flows)}] {summary['Name']}", file=sys.stderr)
        content = describe_flow_content(client, args.instance_id, summary["Id"])
        if content is None:
            continue
        hits = search_attribute(content, attr, args.exact)
        results.append((summary["Name"], hits))

    if args.output_json:
        print(json.dumps(_to_json_bulk(results, attr), indent=2))
    else:
        print_bulk_summary(results, attr)
        if args.detail:
            print_bulk_detail(results, attr)


if __name__ == "__main__":
    main()
