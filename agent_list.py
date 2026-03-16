#!/usr/bin/env python3
"""agent_list.py — List agents in an Amazon Connect instance."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import ct_snapshot

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

CSV_COLUMNS = [
    "Username", "FirstName", "LastName", "Email",
    "RoutingProfile", "SecurityProfiles", "HierarchyGroup",
    "PhoneType", "UserId",
]


# ── AWS client ─────────────────────────────────────────────────────────────────

def make_client(region: str | None, profile: str | None):
    session = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


# ── Data fetchers ──────────────────────────────────────────────────────────────

def list_user_summaries(client, instance_id: str, search: str | None) -> list[dict]:
    """Paginate ListUsers; filter by username substring if --search given."""
    users: list[dict] = []
    token = None
    while True:
        kwargs: dict = {"InstanceId": instance_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_users(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            msg  = e.response["Error"]["Message"]
            print(f"Error listing users [{code}]: {msg}", file=sys.stderr)
            sys.exit(1)
        for u in resp.get("UserSummaryList", []):
            if search and search.lower() not in u["Username"].lower():
                continue
            users.append(u)
        token = resp.get("NextToken")
        if not token:
            break
    return sorted(users, key=lambda u: u["Username"].lower())


def _resolve_name(client, instance_id: str, resource: str, user_id: str, cache: dict,
                  describe_fn, name_key: str) -> str:
    if user_id not in cache:
        try:
            resp = describe_fn(InstanceId=instance_id, **{resource: user_id})
            cache[user_id] = name_key(resp)
        except ClientError:
            cache[user_id] = user_id
    return cache[user_id]


def build_rows(client, instance_id: str, summaries: list[dict]) -> list[dict]:
    rp_cache: dict[str, str] = {}
    hg_cache: dict[str, str] = {}
    sp_cache: dict[str, str] = {}

    rows: list[dict] = []
    total = len(summaries)

    for i, summary in enumerate(summaries, 1):
        print(f"\r  Fetching details... {i}/{total}", end="", flush=True, file=sys.stderr)
        try:
            user = client.describe_user(InstanceId=instance_id, UserId=summary["Id"])["User"]
        except ClientError as e:
            print(f"\nWarning: could not describe {summary['Username']}: {e}", file=sys.stderr)
            continue

        identity = user.get("IdentityInfo", {})
        phone    = user.get("PhoneConfig", {})
        rp_id    = user.get("RoutingProfileId", "")
        hg_id    = user.get("HierarchyGroupId", "")
        sp_ids   = user.get("SecurityProfileIds", [])

        if rp_id:
            if rp_id not in rp_cache:
                try:
                    r = client.describe_routing_profile(InstanceId=instance_id, RoutingProfileId=rp_id)
                    rp_cache[rp_id] = r["RoutingProfile"]["Name"]
                except ClientError:
                    rp_cache[rp_id] = rp_id
            rp_name = rp_cache[rp_id]
        else:
            rp_name = ""

        if hg_id:
            if hg_id not in hg_cache:
                try:
                    r = client.describe_user_hierarchy_group(InstanceId=instance_id, HierarchyGroupId=hg_id)
                    hg_cache[hg_id] = r["HierarchyGroup"]["Name"]
                except ClientError:
                    hg_cache[hg_id] = hg_id
            hg_name = hg_cache[hg_id]
        else:
            hg_name = ""

        sp_names: list[str] = []
        for sp_id in sp_ids:
            if sp_id not in sp_cache:
                try:
                    r = client.describe_security_profile(InstanceId=instance_id, SecurityProfileId=sp_id)
                    sp_cache[sp_id] = r["SecurityProfile"]["SecurityProfileName"]
                except ClientError:
                    sp_cache[sp_id] = sp_id
            sp_names.append(sp_cache[sp_id])

        rows.append({
            "UserId":           user["Id"],
            "Username":         user["Username"],
            "FirstName":        identity.get("FirstName", ""),
            "LastName":         identity.get("LastName", ""),
            "Email":            identity.get("Email", ""),
            "RoutingProfile":   rp_name,
            "SecurityProfiles": "; ".join(sp_names),
            "HierarchyGroup":   hg_name,
            "PhoneType":        phone.get("PhoneType", ""),
        })

    print(file=sys.stderr)  # newline after progress line
    return rows


# ── Output ─────────────────────────────────────────────────────────────────────

def print_table(rows: list[dict]):
    if not rows:
        print("No agents found.")
        return

    cols = ["Username", "FirstName", "LastName", "RoutingProfile", "HierarchyGroup"]
    widths = {c: len(c) for c in cols}
    for row in rows:
        for c in cols:
            widths[c] = max(widths[c], len(str(row.get(c, ""))))

    sep   = "  "
    hdr   = sep.join(f"{c:<{widths[c]}}" for c in cols)
    rule  = sep.join("-" * widths[c] for c in cols)
    print(hdr)
    print(rule)
    for row in rows:
        print(sep.join(f"{str(row.get(c, '')):<{widths[c]}}" for c in cols))
    print(f"\n{len(rows)} agent(s).")


def write_csv(path: Path, rows: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


_MAN = """\
NAME
    agent_list.py — List agents in an Amazon Connect instance

SYNOPSIS
    python agent_list.py --instance-id UUID [OPTIONS]

DESCRIPTION
    Lists all agents (users) in an Amazon Connect instance with their username,
    first/last name, routing profile, hierarchy group, and security profiles.
    Resolves routing profile, hierarchy group, and security profile names via
    describe calls with local caches to minimise API requests. Use --search to
    filter by username substring, --routing-profile to filter by routing profile
    name, --csv to export to file, or --json for machine-readable output.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --search TEXT
        Case-insensitive substring match on username.

    --routing-profile NAME
        Filter by routing profile name (case-insensitive substring).
        Applied after fetching user details.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --csv PATH
        Write results to a CSV file.

    --json
        Print results as JSON (pipe-friendly).

EXAMPLES
    # List all agents (table output)
    python agent_list.py --instance-id <UUID>

    # Search by username
    python agent_list.py --instance-id <UUID> --search jsmith

    # Filter by routing profile
    python agent_list.py --instance-id <UUID> --routing-profile "Basic Routing"

    # Export to CSV
    python agent_list.py --instance-id <UUID> --csv agents.csv

    # JSON output, extract usernames
    python agent_list.py --instance-id <UUID> --json | jq '.[].Username'

IAM PERMISSIONS
    connect:ListUsers
    connect:DescribeUser
    connect:DescribeRoutingProfile
    connect:DescribeUserHierarchyGroup
    connect:DescribeSecurityProfile

NOTES
    Routing profile, hierarchy group, and security profile names are resolved
    using local caches to avoid redundant API calls for users sharing the same
    profile. The --routing-profile filter is applied client-side after all user
    details have been fetched.
"""

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    p = argparse.ArgumentParser(
        description="List agents in an Amazon Connect instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID>
  %(prog)s --instance-id <UUID> --search jsmith
  %(prog)s --instance-id <UUID> --routing-profile "Basic Routing"
  %(prog)s --instance-id <UUID> --csv agents.csv
  %(prog)s --instance-id <UUID> --json | jq '.[].Username'
        """,
    )
    p.add_argument("--instance-id",      required=True, metavar="UUID")
    p.add_argument("--search",           default=None,  metavar="TEXT",
                   help="Case-insensitive substring match on username")
    p.add_argument("--routing-profile",  default=None,  metavar="NAME",
                   help="Filter by routing profile name (case-insensitive substring)")
    p.add_argument("--region",           default=None)
    p.add_argument("--profile",          default=None)
    p.add_argument("--csv",              default=None,  metavar="PATH", dest="csv_path",
                   help="Write results to a CSV file")
    p.add_argument("--json",             action="store_true", dest="json_out",
                   help="Print results as JSON (pipe-friendly)")
    args = p.parse_args()

    client = make_client(args.region, args.profile)

    print(f"Listing users...", end=" ", flush=True, file=sys.stderr)
    summaries = list_user_summaries(client, args.instance_id, args.search)
    print(f"{len(summaries)} matched.", file=sys.stderr)

    if not summaries:
        print("No agents matched.")
        sys.exit(0)

    rows = build_rows(client, args.instance_id, summaries)

    if args.routing_profile:
        needle = args.routing_profile.lower()
        rows = [r for r in rows if needle in r["RoutingProfile"].lower()]

    if not rows:
        print("No agents matched after filtering.")
        sys.exit(0)

    if args.json_out:
        print(json.dumps(rows, indent=2))
        return

    if args.csv_path:
        csv_dest = ct_snapshot.output_path("agent_list", args.csv_path)
        write_csv(csv_dest, rows)
        print(f"Wrote {len(rows)} rows → {csv_dest}")
        return

    print_table(rows)


if __name__ == "__main__":
    main()
