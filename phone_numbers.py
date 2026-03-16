#!/usr/bin/env python3
"""phone_numbers.py — List all claimed phone numbers and their associated contact flows.

Uses ListPhoneNumbersV2 to fetch every claimed number on an Amazon Connect
instance and resolves the associated contact flow name for each one.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import ct_snapshot

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

_MAN = """\
NAME
    phone_numbers.py — List all claimed phone numbers and their associated contact flows

SYNOPSIS
    python phone_numbers.py --instance-id UUID [OPTIONS]

DESCRIPTION
    Fetches every claimed phone number on an Amazon Connect instance and
    resolves the contact flow each number routes to. Uses the instance
    snapshot for fast name resolution when available; falls back to
    DescribeContactFlow per number otherwise.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --flow NAME
        Filter to numbers associated with a specific flow (case-insensitive substring).

    --unassigned
        Show only numbers with no contact flow assigned.

    --csv FILE
        Write results to a CSV file.

    --json
        Print results as JSON to stdout.

EXAMPLES
    # All phone numbers with their flows
    python phone_numbers.py --instance-id <UUID> --region us-east-1

    # Numbers routed to a specific flow
    python phone_numbers.py --instance-id <UUID> --flow "Main IVR"

    # Unassigned numbers only
    python phone_numbers.py --instance-id <UUID> --unassigned

    # Export to CSV
    python phone_numbers.py --instance-id <UUID> --csv phone_numbers.csv

    # JSON output
    python phone_numbers.py --instance-id <UUID> --json | jq '.[] | select(.flow == null)'

IAM PERMISSIONS
    connect:ListPhoneNumbersV2
    connect:DescribeContactFlow  (only when snapshot is absent or stale)

NOTES
    Run instance_snapshot.py first to enable fast offline flow name resolution.
    Without a snapshot, one DescribeContactFlow API call is made per unique
    flow ARN (results are cached within the run).
"""


# ── Argument parsing ───────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="List all claimed phone numbers and their associated contact flows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --flow "Main IVR"
  %(prog)s --instance-id <UUID> --unassigned
  %(prog)s --instance-id <UUID> --csv phone_numbers.csv
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--region",      default=None, help="AWS region")
    p.add_argument("--profile",     default=None, help="AWS named profile")
    p.add_argument("--flow",        default=None, metavar="NAME",
                   help="Filter by associated flow name (case-insensitive substring)")
    p.add_argument("--unassigned",  action="store_true",
                   help="Show only numbers with no flow assigned")
    p.add_argument("--csv",  default=None, metavar="FILE", help="Write CSV to file")
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Print JSON to stdout")
    return p.parse_args()


# ── Client factory ─────────────────────────────────────────────────────────────

def make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_phone_numbers(client, instance_id) -> list:
    """Fetch all claimed phone numbers via ListPhoneNumbersV2."""
    items, token = [], None
    while True:
        kwargs = {"InstanceId": instance_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_phone_numbers_v2(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            print(f"Error listing phone numbers [{code}]: {e.response['Error']['Message']}",
                  file=sys.stderr)
            sys.exit(1)
        items.extend(resp.get("ListPhoneNumbersSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return items


def resolve_flow_name(client, instance_id, target_arn, cache, snapshot) -> str | None:
    """Resolve a TargetArn to a contact flow name."""
    if not target_arn:
        return None
    if target_arn in cache:
        return cache[target_arn]

    # Try snapshot first
    if snapshot:
        name = ct_snapshot.resolve(snapshot, "flows", target_arn)
        if name:
            cache[target_arn] = name
            return name

    # Fall back to DescribeContactFlow
    try:
        flow_id = target_arn.split("/")[-1]
        resp    = client.describe_contact_flow(InstanceId=instance_id, ContactFlowId=flow_id)
        name    = resp["ContactFlow"]["Name"]
        cache[target_arn] = name
        return name
    except ClientError:
        cache[target_arn] = None
        return None


# ── Output ─────────────────────────────────────────────────────────────────────

def _hr():
    print("  " + "─" * 72)


def print_human(rows, instance_id):
    total      = len(rows)
    unassigned = sum(1 for r in rows if not r["flow"])

    _hr()
    print(f"  PHONE NUMBERS   {instance_id}")
    _hr()
    print(f"  {total} number(s)  ·  {unassigned} unassigned\n")

    # Column widths
    num_w  = max((len(r["number"]) for r in rows), default=12)
    flow_w = max((len(r["flow"] or "(unassigned)") for r in rows), default=16)
    flow_w = max(flow_w, 16)

    print(f"  {'NUMBER':<{num_w}}  {'TYPE':<10}  {'COUNTRY':<7}  {'FLOW'}")
    print(f"  {'─'*num_w}  {'─'*10}  {'─'*7}  {'─'*flow_w}")

    for r in rows:
        flow_label = r["flow"] or "\033[90m(unassigned)\033[0m"
        status_str = f"  \033[33m[{r['status']}]\033[0m" if r["status"] != "CLAIMED" else ""
        print(f"  {r['number']:<{num_w}}  {r['type']:<10}  {r['country']:<7}  {flow_label}{status_str}")

    print()
    _hr()
    print()


def write_csv(rows, path):
    fields = ["number", "type", "country", "flow", "status", "phone_number_id", "target_arn"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    args   = parse_args()
    client = make_client(args.region, args.profile)

    # Load snapshot for fast name resolution (optional)
    snapshot = ct_snapshot.load(args.instance_id)
    if snapshot:
        ct_snapshot.warn_if_stale(snapshot)
    else:
        print("  Note: no instance snapshot found — flow names resolved via API.",
              file=sys.stderr)

    print(f"  Fetching phone numbers...", file=sys.stderr)
    raw = fetch_phone_numbers(client, args.instance_id)
    print(f"  {len(raw)} number(s) found. Resolving flow names...", file=sys.stderr)

    flow_cache: dict = {}
    rows = []
    for item in raw:
        target_arn = item.get("TargetArn") or ""
        flow_name  = resolve_flow_name(client, args.instance_id, target_arn,
                                       flow_cache, snapshot)
        status_obj = item.get("PhoneNumberStatus") or {}
        rows.append({
            "number":          item.get("PhoneNumber", ""),
            "type":            item.get("PhoneNumberType", ""),
            "country":         item.get("PhoneNumberCountryCode", ""),
            "flow":            flow_name,
            "status":          status_obj.get("Value", "CLAIMED"),
            "phone_number_id": item.get("PhoneNumberId", ""),
            "target_arn":      target_arn,
        })

    # Sort by phone number
    rows.sort(key=lambda r: r["number"])

    # Apply filters
    if args.unassigned:
        rows = [r for r in rows if not r["flow"]]
    elif args.flow:
        needle = args.flow.lower()
        rows   = [r for r in rows if r["flow"] and needle in r["flow"].lower()]

    if args.output_json:
        print(json.dumps(rows, indent=2))
        return

    print_human(rows, args.instance_id)

    if args.csv:
        dest = ct_snapshot.output_path("phone_numbers", args.csv)
        write_csv(rows, dest)
        print(f"  Saved → {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
