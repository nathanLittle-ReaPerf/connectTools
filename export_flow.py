#!/usr/bin/env python3
"""export-flow: Export an Amazon Connect contact flow to JSON by name."""

from __future__ import annotations

import argparse
import json
import re
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

FLOW_TYPES = [
    "CONTACT_FLOW",
    "CUSTOMER_QUEUE",
    "CUSTOMER_HOLD",
    "CUSTOMER_WHISPER",
    "AGENT_HOLD",
    "AGENT_WHISPER",
    "OUTBOUND_WHISPER",
    "AGENT_TRANSFER",
    "QUEUE_TRANSFER",
    "CAMPAIGN",
]


# ── Argument parsing ─────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Export an Amazon Connect contact flow to JSON by name.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Export a flow to <Flow Name>.json
  %(prog)s --instance-id <UUID> --name "Main IVR" --region us-east-1

  # Exact name match (default is case-insensitive substring)
  %(prog)s --instance-id <UUID> --name "Main IVR" --exact

  # Write to a specific file
  %(prog)s --instance-id <UUID> --name "Main IVR" --output ./flows/main_ivr.json

  # Print to stdout (pipe-friendly)
  %(prog)s --instance-id <UUID> --name "Main IVR" --stdout

  # List all flows (with optional name filter) without exporting
  %(prog)s --instance-id <UUID> --list
  %(prog)s --instance-id <UUID> --list --name "IVR"
  %(prog)s --instance-id <UUID> --list --type CONTACT_FLOW
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--region", default=None, help="AWS region (defaults to session/CloudShell region)")
    p.add_argument("--profile", default=None, help="AWS named profile")

    # Name / ARN matching (mutually exclusive)
    search = p.add_mutually_exclusive_group()
    search.add_argument("--name", metavar="NAME", help="Flow name to search for (case-insensitive substring by default)")
    search.add_argument("--arn", metavar="ARN", help="Full or partial flow ARN (exact match)")
    p.add_argument("--exact", action="store_true", help="Require an exact name match (case-insensitive); no effect with --arn")

    # Type filter
    p.add_argument(
        "--type",
        metavar="TYPE",
        choices=FLOW_TYPES,
        help=f"Restrict search to one flow type. Choices: {', '.join(FLOW_TYPES)}",
    )

    # Output
    out = p.add_mutually_exclusive_group()
    out.add_argument("--output", metavar="FILE", help="Write exported JSON to this file path")
    out.add_argument("--stdout", action="store_true", help="Print exported JSON to stdout")

    # List mode
    p.add_argument("--list", action="store_true", help="List matching flows without exporting")

    args = p.parse_args()

    if not args.list and not args.name and not args.arn:
        p.error("--name or --arn is required unless --list is used")

    return args


# ── AWS helpers ──────────────────────────────────────────────────────────────

def make_client(region, profile):
    session = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


def list_all_flows(client, instance_id, flow_types=None):
    """Paginate ListContactFlows and return all flow summaries."""
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
            msg = e.response["Error"]["Message"]
            print(f"Error listing flows [{code}]: {msg}", file=sys.stderr)
            sys.exit(1)
        flows.extend(resp.get("ContactFlowSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return flows


def describe_flow(client, instance_id, flow_id):
    """Fetch full contact flow definition including Content."""
    try:
        return client.describe_contact_flow(
            InstanceId=instance_id, ContactFlowId=flow_id
        )["ContactFlow"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"]["Message"]
        print(f"Error describing flow [{code}]: {msg}", file=sys.stderr)
        sys.exit(1)


# ── Matching ──────────────────────────────────────────────────────────────────

def match_flows(flows, name, exact):
    name_lower = name.lower()
    if exact:
        return [f for f in flows if f["Name"].lower() == name_lower]
    return [f for f in flows if name_lower in f["Name"].lower()]


def match_by_arn(flows, arn):
    """Exact ARN match. ARNs are unique identifiers so no ambiguity handling needed."""
    return [f for f in flows if f.get("Arn") == arn]


# ── Output helpers ────────────────────────────────────────────────────────────

def safe_filename(name):
    """Turn a flow name into a safe filename."""
    return re.sub(r"[^\w\-]", "_", name).strip("_") + ".json"


def write_flow(flow_def, output_path, to_stdout):
    """
    Parse the Content field (a JSON string) and write pretty-printed JSON.
    Includes a metadata envelope so the file is self-describing.
    """
    try:
        content = json.loads(flow_def["Content"])
    except (json.JSONDecodeError, KeyError):
        content = flow_def.get("Content", "")

    export = {
        "metadata": {
            "name": flow_def.get("Name"),
            "id": flow_def.get("Id"),
            "arn": flow_def.get("Arn"),
            "type": flow_def.get("Type"),
            "status": flow_def.get("Status"),
            "state": flow_def.get("State"),
            "description": flow_def.get("Description"),
            "last_modified_time": str(flow_def.get("LastModifiedTime", "")),
            "last_modified_region": flow_def.get("LastModifiedRegion"),
            "flow_content_sha256": flow_def.get("FlowContentSha256"),
        },
        "content": content,
    }

    payload = json.dumps(export, indent=2)

    if to_stdout:
        print(payload)
        return None

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(payload)
    return output_path


# ── List display ──────────────────────────────────────────────────────────────

def print_flow_list(flows):
    if not flows:
        print("No flows found.")
        return
    # Column widths
    name_w  = max(len(f["Name"]) for f in flows)
    type_w  = max(len(f.get("ContactFlowType", "")) for f in flows)
    name_w  = max(name_w, 4)
    type_w  = max(type_w, 4)

    header = f"  {'Name':<{name_w}}  {'Type':<{type_w}}  Status      State"
    print(header)
    print("  " + "─" * (len(header) - 2))
    for f in sorted(flows, key=lambda x: x["Name"].lower()):
        print(
            f"  {f['Name']:<{name_w}}  "
            f"{f.get('ContactFlowType', ''):<{type_w}}  "
            f"{f.get('ContactFlowStatus', ''):<10}  "
            f"{f.get('ContactFlowState', '')}"
        )
        print(f"  {'':>{name_w}}  {f.get('Arn', '')}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    client = make_client(args.region, args.profile)

    flow_types = [args.type] if args.type else None
    all_flows = list_all_flows(client, args.instance_id, flow_types)

    # ── List mode ────────────────────────────────────────────────────────────
    if args.list:
        if args.arn:
            flows = match_by_arn(all_flows, args.arn)
            label = f"  {len(flows)} flow(s) matching ARN '{args.arn}'"
        elif args.name:
            flows = match_flows(all_flows, args.name, args.exact)
            label = f"  {len(flows)} flow(s) matching '{args.name}'"
        else:
            flows = all_flows
            label = f"  {len(flows)} flow(s)"
        if args.type:
            label += f" of type {args.type}"

        if args.stdout:
            print(json.dumps(flows, indent=2, default=str))
        elif args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(flows, f, indent=2, default=str)
            print(f"{label} → {args.output}")
        else:
            print(label)
            print()
            print_flow_list(flows)
        return

    # ── Export mode ──────────────────────────────────────────────────────────
    if args.arn:
        matches = match_by_arn(all_flows, args.arn)
        if not matches:
            print(f"No flow found with ARN '{args.arn}'.\nUse --list to browse available flows.", file=sys.stderr)
            sys.exit(1)
        # ARNs are unique — no multi-match case possible
    else:
        matches = match_flows(all_flows, args.name, args.exact)

        if not matches:
            hint = "exact " if args.exact else ""
            type_hint = f" of type {args.type}" if args.type else ""
            print(
                f"No flows found with {hint}name '{args.name}'{type_hint}.\n"
                f"Use --list to browse available flows.",
                file=sys.stderr,
            )
            sys.exit(1)

        if len(matches) > 1:
            print(
                f"Found {len(matches)} flows matching '{args.name}'. "
                f"Use --exact, --arn, or a more specific name:\n",
                file=sys.stderr,
            )
            for f in matches:
                print(f"  {f['Name']!r}  ({f.get('ContactFlowType', '?')})  {f.get('Arn', f['Id'])}", file=sys.stderr)
            sys.exit(1)

    flow_summary = matches[0]
    flow_def = describe_flow(client, args.instance_id, flow_summary["Id"])

    # Determine output destination
    to_stdout = args.stdout
    if not to_stdout:
        output_path = args.output or safe_filename(flow_def["Name"])
    else:
        output_path = None

    written = write_flow(flow_def, output_path, to_stdout)

    if written:
        print(f"Exported '{flow_def['Name']}' → {written}")
        print(f"  Type:   {flow_def.get('Type', '?')}")
        print(f"  Status: {flow_def.get('Status', '?')}")
        print(f"  State:  {flow_def.get('State', '?')}")


if __name__ == "__main__":
    main()
