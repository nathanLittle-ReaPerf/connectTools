#!/usr/bin/env python3
"""security_profile_diff.py — Compare permissions between two Amazon Connect security profiles."""

from __future__ import annotations

import argparse
import csv
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

_MAN = """\
NAME
    security_profile_diff.py — Compare permissions between two Amazon Connect security profiles

SYNOPSIS
    python security_profile_diff.py --instance-id UUID --profile-a NAME --profile-b NAME [OPTIONS]

DESCRIPTION
    Fetches the permission sets for two security profiles and diffs them.
    Shows permissions only in A, only in B, and shared by both. Use --all
    to show every permission for both profiles side by side.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --profile-a SUBSTR
        Name (or case-insensitive substring) of the first security profile. Required.

    --profile-b SUBSTR
        Name (or case-insensitive substring) of the second security profile. Required.

    --all
        Show all permissions for both profiles, not just the diff.

    --region REGION
        AWS region. Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --csv FILE
        Write diff to CSV.

    --json
        Print JSON to stdout.

EXAMPLES
    python security_profile_diff.py --instance-id <UUID> --profile-a "Agent" --profile-b "Supervisor"
    python security_profile_diff.py --instance-id <UUID> --profile-a "Agent" --profile-b "Admin" --all
    python security_profile_diff.py --instance-id <UUID> --profile-a "Tier 1" --profile-b "Tier 2" --json

IAM PERMISSIONS
    connect:ListSecurityProfiles
    connect:ListSecurityProfilePermissions
    connect:DescribeSecurityProfile
"""


# ── Argument parsing ───────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Compare permissions between two Amazon Connect security profiles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--instance-id",  required=True, metavar="UUID")
    p.add_argument("--profile-a",    required=True, metavar="SUBSTR")
    p.add_argument("--profile-b",    required=True, metavar="SUBSTR")
    p.add_argument("--all",          action="store_true", dest="show_all",
                   help="Show all permissions, not just differences")
    p.add_argument("--region",       default=None)
    p.add_argument("--profile",      default=None, help="AWS named profile")
    p.add_argument("--csv",          default=None, metavar="FILE")
    p.add_argument("--json",         action="store_true", dest="output_json")
    return p.parse_args()


# ── Client factory ─────────────────────────────────────────────────────────────

def make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


# ── Data fetchers ──────────────────────────────────────────────────────────────

def list_security_profiles(client, instance_id):
    profiles, token = [], None
    while True:
        kwargs = {"InstanceId": instance_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_security_profiles(**kwargs)
        except ClientError as e:
            _fatal("ListSecurityProfiles", e)
        profiles.extend(resp.get("SecurityProfileSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            break
    return profiles


def resolve_profile(profiles, substr, label):
    """Find a single security profile matching substr. Exits on 0 or >1 matches."""
    matches = [p for p in profiles if substr.lower() in p["Name"].lower()]
    if not matches:
        print(f"  Error: no security profile matching {label}='{substr}'.", file=sys.stderr)
        print(f"  Available profiles:", file=sys.stderr)
        for p in sorted(profiles, key=lambda x: x["Name"].lower()):
            print(f"    {p['Name']}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"  Error: '{substr}' matches {len(matches)} profiles for {label}. Be more specific:", file=sys.stderr)
        for m in matches:
            print(f"    {m['Name']}", file=sys.stderr)
        sys.exit(1)
    return matches[0]


def list_permissions(client, instance_id, sp_id):
    """Return sorted list of permission strings for a security profile."""
    perms, token = [], None
    while True:
        kwargs = {"InstanceId": instance_id, "SecurityProfileId": sp_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_security_profile_permissions(**kwargs)
        except ClientError as e:
            _fatal("ListSecurityProfilePermissions", e)
        perms.extend(resp.get("Permissions", []))
        token = resp.get("NextToken")
        if not token:
            break
    return sorted(set(perms))


def describe_profile(client, instance_id, sp_id):
    try:
        return client.describe_security_profile(
            InstanceId=instance_id, SecurityProfileId=sp_id
        ).get("SecurityProfile", {})
    except ClientError:
        return {}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fatal(op, e):
    code = e.response["Error"]["Code"]
    msg  = e.response["Error"]["Message"]
    print(f"Error in {op} [{code}]: {msg}", file=sys.stderr)
    sys.exit(1)


def _hr(char="─", width=68):
    print("  " + char * width)


# ANSI helpers
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_DIM    = "\033[90m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"


def _g(s): return f"{_GREEN}{s}{_RESET}"
def _r(s): return f"{_RED}{s}{_RESET}"
def _d(s): return f"{_DIM}{s}{_RESET}"
def _b(s): return f"{_BOLD}{s}{_RESET}"


# ── Output ─────────────────────────────────────────────────────────────────────

def print_human(result, show_all):
    a_name   = result["profile_a"]["name"]
    b_name   = result["profile_b"]["name"]
    only_a   = result["only_in_a"]
    only_b   = result["only_in_b"]
    shared   = result["shared"]

    _hr()
    print(f"  {_b('SECURITY PROFILE DIFF')}")
    _hr()
    print(f"\n  A  {_b(a_name)}   ({result['profile_a']['permission_count']} permissions)")
    print(f"  B  {_b(b_name)}   ({result['profile_b']['permission_count']} permissions)\n")

    if not only_a and not only_b:
        print(f"  {_d('Profiles are identical — same permission set.')}\n")
        _hr()
        print()
        return

    col = 52  # permission column width

    # Only in A
    if only_a:
        print(f"  {_r('─ Only in A ─')}  ({len(only_a)})\n")
        for p in only_a:
            print(f"  {_r('─')}  {p}")
        print()

    # Only in B
    if only_b:
        print(f"  {_g('+ Only in B +')}  ({len(only_b)})\n")
        for p in only_b:
            print(f"  {_g('+')}  {p}")
        print()

    # Shared
    if show_all and shared:
        print(f"  {_d('= Shared =')}  ({len(shared)})\n")
        for p in shared:
            print(f"  {_d('=')}  {_d(p)}")
        print()
    elif shared:
        print(f"  {_d(f'{len(shared)} permissions shared (use --all to list them)')}")
        print()

    _hr()
    print(f"\n  Summary:  {_r(f'{len(only_a)} only in A')}  |  "
          f"{_g(f'{len(only_b)} only in B')}  |  "
          f"{_d(f'{len(shared)} shared')}\n")


def write_csv(result, path):
    rows = []
    for p in result["only_in_a"]:
        rows.append({"Permission": p, "InA": "yes", "InB": "no",  "Status": "only_in_a"})
    for p in result["only_in_b"]:
        rows.append({"Permission": p, "InA": "no",  "InB": "yes", "Status": "only_in_b"})
    for p in result["shared"]:
        rows.append({"Permission": p, "InA": "yes", "InB": "yes", "Status": "shared"})
    rows.sort(key=lambda r: r["Permission"])

    fieldnames = ["Permission", "InA", "InB", "Status"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved → {path}", file=sys.stderr)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args   = parse_args()
    client = make_client(args.region, args.profile)

    print("  Loading security profiles...", file=sys.stderr)
    all_profiles = list_security_profiles(client, args.instance_id)

    sp_a = resolve_profile(all_profiles, args.profile_a, "--profile-a")
    sp_b = resolve_profile(all_profiles, args.profile_b, "--profile-b")

    if sp_a["Id"] == sp_b["Id"]:
        print("  Error: --profile-a and --profile-b resolved to the same profile.", file=sys.stderr)
        sys.exit(1)

    print(f"  Fetching permissions for '{sp_a['Name']}'...", file=sys.stderr)
    perms_a = list_permissions(client, args.instance_id, sp_a["Id"])

    print(f"  Fetching permissions for '{sp_b['Name']}'...", file=sys.stderr)
    perms_b = list_permissions(client, args.instance_id, sp_b["Id"])

    set_a, set_b = set(perms_a), set(perms_b)
    result = {
        "profile_a": {
            "id":               sp_a["Id"],
            "name":             sp_a["Name"],
            "permissions":      perms_a,
            "permission_count": len(perms_a),
        },
        "profile_b": {
            "id":               sp_b["Id"],
            "name":             sp_b["Name"],
            "permissions":      perms_b,
            "permission_count": len(perms_b),
        },
        "only_in_a": sorted(set_a - set_b),
        "only_in_b": sorted(set_b - set_a),
        "shared":    sorted(set_a & set_b),
        "identical": set_a == set_b,
    }

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)
        print(json.dumps(result, indent=2, default=serial))
    elif args.csv:
        write_csv(result, args.csv)
    else:
        print_human(result, args.show_all)


if __name__ == "__main__":
    main()
