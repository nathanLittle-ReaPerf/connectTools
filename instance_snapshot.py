#!/usr/bin/env python3
"""instance_snapshot.py — Fetch and store an Amazon Connect instance inventory.

Pulls all listable resources (queues, flows, routing profiles, users, etc.)
and saves them to ~/.connecttools/snapshot_<instance-id>.json for use by
other tools as a fast, offline name-resolution cache.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import ct_snapshot

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

RESOURCE_LABELS = {
    "queues":             "Queues",
    "flows":              "Contact flows",
    "routing_profiles":   "Routing profiles",
    "hours_of_operation": "Hours of operation",
    "prompts":            "Prompts",
    "quick_connects":     "Quick connects",
    "security_profiles":  "Security profiles",
    "phone_numbers":      "Phone numbers",
    "users":              "Users",
}


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Fetch and store an Amazon Connect instance inventory snapshot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Fetch and save snapshot
  %(prog)s --instance-id <UUID> --region us-east-1

  # Show summary of stored snapshot (no API calls)
  %(prog)s --instance-id <UUID> --show

  # Search the snapshot for a resource by name
  %(prog)s --instance-id <UUID> --lookup queues "Billing"
  %(prog)s --instance-id <UUID> --lookup flows "IVR"
  %(prog)s --instance-id <UUID> --lookup users "jsmith"

  # Dump full snapshot as JSON to stdout
  %(prog)s --instance-id <UUID> --json
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--region",  default=None, help="AWS region")
    p.add_argument("--profile", default=None, help="AWS named profile")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--show",   action="store_true",
                      help="Print summary of stored snapshot (no API calls)")
    mode.add_argument("--json",   action="store_true", dest="output_json",
                      help="Dump full snapshot as JSON to stdout (no refresh)")
    mode.add_argument("--lookup", nargs=2, metavar=("TYPE", "NAME"),
                      help="Search snapshot by resource type and name fragment")
    return p.parse_args()


# ── Client factory ────────────────────────────────────────────────────────────

def make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


# ── Generic paginator ─────────────────────────────────────────────────────────

def _paginate(client, method: str, list_key: str, **kwargs) -> list:
    items, token = [], None
    while True:
        if token:
            kwargs["NextToken"] = token
        try:
            resp = getattr(client, method)(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            # Some resources may not exist in all configurations
            if code in ("ResourceNotFoundException", "InvalidParameterException",
                        "AccessDeniedException", "UnsupportedOperationException"):
                return []
            raise
        items.extend(resp.get(list_key, []))
        token = resp.get("NextToken")
        if not token:
            return items


# ── Fetchers (each returns dict keyed by resource ID) ─────────────────────────

def fetch_queues(client, instance_id) -> dict:
    items = _paginate(client, "list_queues", "QueueSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {
        i["Id"]: {
            "id":   i["Id"],
            "arn":  i.get("Arn", ""),
            "name": i.get("Name", ""),
            "type": i.get("QueueType", ""),
        }
        for i in items
    }


def fetch_flows(client, instance_id) -> dict:
    items = _paginate(client, "list_contact_flows", "ContactFlowSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {
        i["Id"]: {
            "id":     i["Id"],
            "arn":    i.get("Arn", ""),
            "name":   i.get("Name", ""),
            "type":   i.get("ContactFlowType", ""),
            "status": i.get("ContactFlowStatus", ""),
            "state":  i.get("ContactFlowState", ""),
        }
        for i in items
    }


def fetch_routing_profiles(client, instance_id) -> dict:
    items = _paginate(client, "list_routing_profiles", "RoutingProfileSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {
        i["Id"]: {"id": i["Id"], "arn": i.get("Arn", ""), "name": i.get("Name", "")}
        for i in items
    }


def fetch_hours(client, instance_id) -> dict:
    items = _paginate(client, "list_hours_of_operations", "HoursOfOperationSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {
        i["Id"]: {"id": i["Id"], "arn": i.get("Arn", ""), "name": i.get("Name", "")}
        for i in items
    }


def fetch_prompts(client, instance_id) -> dict:
    items = _paginate(client, "list_prompts", "PromptSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {
        i["Id"]: {"id": i["Id"], "arn": i.get("Arn", ""), "name": i.get("Name", "")}
        for i in items
    }


def fetch_quick_connects(client, instance_id) -> dict:
    items = _paginate(client, "list_quick_connects", "QuickConnectSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {
        i["Id"]: {
            "id":   i["Id"],
            "arn":  i.get("Arn", ""),
            "name": i.get("Name", ""),
            "type": i.get("QuickConnectType", ""),
        }
        for i in items
    }


def fetch_security_profiles(client, instance_id) -> dict:
    items = _paginate(client, "list_security_profiles", "SecurityProfileSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {
        i["Id"]: {"id": i["Id"], "arn": i.get("Arn", ""), "name": i.get("Name", "")}
        for i in items
    }


def fetch_phone_numbers(client, instance_id) -> dict:
    items = _paginate(client, "list_phone_numbers", "PhoneNumberSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {
        i["Id"]: {
            "id":           i["Id"],
            "arn":          i.get("Arn", ""),
            "name":         i.get("PhoneNumber", ""),   # use number as the display name
            "number":       i.get("PhoneNumber", ""),
            "type":         i.get("PhoneNumberType", ""),
            "country_code": i.get("PhoneNumberCountryCode", ""),
        }
        for i in items
    }


def fetch_users(client, instance_id) -> dict:
    items = _paginate(client, "list_users", "UserSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {
        i["Id"]: {
            "id":       i["Id"],
            "arn":      i.get("Arn", ""),
            "username": i.get("Username", ""),
            "name":     i.get("Username", ""),   # fallback; full name requires DescribeUser
        }
        for i in items
    }


FETCHERS = [
    ("queues",             fetch_queues),
    ("flows",              fetch_flows),
    ("routing_profiles",   fetch_routing_profiles),
    ("hours_of_operation", fetch_hours),
    ("prompts",            fetch_prompts),
    ("quick_connects",     fetch_quick_connects),
    ("security_profiles",  fetch_security_profiles),
    ("phone_numbers",      fetch_phone_numbers),
    ("users",              fetch_users),
]


# ── Fetch full snapshot ───────────────────────────────────────────────────────

def fetch_snapshot(client, instance_id) -> dict:
    # Get instance metadata
    alias = None
    try:
        inst  = client.describe_instance(InstanceId=instance_id)["Instance"]
        alias = inst.get("InstanceAlias")
    except ClientError:
        pass

    snapshot = {
        "instance_id":    instance_id,
        "instance_alias": alias or "",
        "fetched_at":     dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    for resource_type, fetcher in FETCHERS:
        label = RESOURCE_LABELS.get(resource_type, resource_type)
        print(f"  Fetching {label}...", file=sys.stderr)
        try:
            snapshot[resource_type] = fetcher(client, instance_id)
        except ClientError as e:
            print(f"    Warning: {e.response['Error']['Message']}", file=sys.stderr)
            snapshot[resource_type] = {}

    return snapshot


# ── Output helpers ────────────────────────────────────────────────────────────

def _hr():
    print("  " + "─" * 60)


def print_summary(snapshot: dict):
    alias     = snapshot.get("instance_alias") or snapshot.get("instance_id", "?")
    iid       = snapshot.get("instance_id", "?")
    age       = ct_snapshot.age_hours(snapshot)
    age_str   = f"{int(age)}h ago" if age < 48 else f"{int(age/24)}d ago"
    fetched   = snapshot.get("fetched_at", "?")[:19].replace("T", " ")

    _hr()
    print(f"  INSTANCE SNAPSHOT   {alias}")
    _hr()
    print(f"  Instance ID  : {iid}")
    print(f"  Fetched      : {fetched} UTC  ({age_str})")
    print(f"  Stored at    : {ct_snapshot.snapshot_path(iid)}")
    print()

    counts = ct_snapshot.counts(snapshot)
    for resource_type, label in RESOURCE_LABELS.items():
        n = counts.get(resource_type, 0)
        print(f"  {label:<24}  {n:>5}")

    _hr()
    print()


def print_lookup(results: list, resource_type: str, name_fragment: str):
    if not results:
        print(f"  No {resource_type} found matching {name_fragment!r}.")
        return
    print(f"  {len(results)} result(s) for {resource_type!r} matching {name_fragment!r}:\n")
    for item in results:
        display = item.get("name") or item.get("username") or item.get("number") or "?"
        print(f"  {display}")
        print(f"    ID  : {item.get('id', '?')}")
        print(f"    ARN : {item.get('arn', '?')}")
        if item.get("type"):
            print(f"    Type: {item['type']}")
        if item.get("number") and item.get("number") != display:
            print(f"    Num : {item['number']}")
        print()


_MAN = """\
NAME
    instance_snapshot.py — Fetch and store an Amazon Connect instance inventory

SYNOPSIS
    python instance_snapshot.py --instance-id UUID [OPTIONS]

DESCRIPTION
    Pulls all listable resources from an Amazon Connect instance (queues, flows,
    routing profiles, hours of operation, prompts, quick connects, security profiles,
    phone numbers, and users) and saves them to ~/.connecttools/snapshot_<instance-id>.json.
    This snapshot is used by other tools (flow_scan.py, etc.) as an offline
    name-resolution cache. Use --show to view a stored snapshot, --lookup to search
    by resource type and name, or --json to dump the full snapshot as JSON.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --show
        Print a summary of the stored snapshot (resource counts, last refreshed).
        No API calls are made. Mutually exclusive with --json and --lookup.

    --json
        Dump the full stored snapshot as JSON to stdout. No refresh.
        Mutually exclusive with --show and --lookup.

    --lookup TYPE NAME
        Search the snapshot for a resource by type and name fragment.
        TYPE examples: queues, flows, routing_profiles, users, prompts.
        Mutually exclusive with --show and --json.

EXAMPLES
    # Fetch and save snapshot
    python instance_snapshot.py --instance-id <UUID> --region us-east-1

    # Show summary of stored snapshot (no API calls)
    python instance_snapshot.py --instance-id <UUID> --show

    # Search for a queue by name
    python instance_snapshot.py --instance-id <UUID> --lookup queues "Billing"

    # Search for a flow
    python instance_snapshot.py --instance-id <UUID> --lookup flows "IVR"

    # Dump full snapshot as JSON
    python instance_snapshot.py --instance-id <UUID> --json

IAM PERMISSIONS
    connect:ListQueues
    connect:ListContactFlows
    connect:ListRoutingProfiles
    connect:ListHoursOfOperations
    connect:ListPrompts
    connect:ListQuickConnects
    connect:ListSecurityProfiles
    connect:ListPhoneNumbers
    connect:ListUsers

NOTES
    The snapshot is stored at ~/.connecttools/snapshot_<instance-id>.json.
    Tools that use the snapshot for name resolution will warn if the snapshot
    is older than 24 hours. Run this tool again to refresh it.
"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    args = parse_args()

    # ── Read-only modes (no API calls) ────────────────────────────────────────
    if args.show or args.output_json or args.lookup:
        snapshot = ct_snapshot.load(args.instance_id)
        if snapshot is None:
            print(
                f"No snapshot found for {args.instance_id}.\n"
                f"Run without --show/--json/--lookup to fetch one.",
                file=sys.stderr,
            )
            sys.exit(1)

        if args.output_json:
            print(json.dumps(snapshot, indent=2))
            return

        if args.show:
            print_summary(snapshot)
            return

        if args.lookup:
            resource_type, name_fragment = args.lookup
            if resource_type not in RESOURCE_LABELS:
                valid = ", ".join(RESOURCE_LABELS.keys())
                print(f"Error: unknown resource type {resource_type!r}.\nValid types: {valid}",
                      file=sys.stderr)
                sys.exit(1)
            ct_snapshot.warn_if_stale(snapshot)
            results = ct_snapshot.search(snapshot, resource_type, name_fragment)
            print_lookup(results, resource_type, name_fragment)
            return

    # ── Fetch mode ────────────────────────────────────────────────────────────
    client   = make_client(args.region, args.profile)
    snapshot = fetch_snapshot(client, args.instance_id)
    path     = ct_snapshot.save(args.instance_id, snapshot)

    counts = ct_snapshot.counts(snapshot)
    total  = sum(counts.values())
    print(f"  Saved {total} resources → {path}", file=sys.stderr)

    print_summary(snapshot)


if __name__ == "__main__":
    main()
