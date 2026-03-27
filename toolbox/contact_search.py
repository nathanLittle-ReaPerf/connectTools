#!/usr/bin/env python3
"""contact-search: Search Amazon Connect contacts by time range and optional filters; export to CSV."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import ct_snapshot

RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

_MAN = """\
NAME
    contact_search.py — Search Amazon Connect contacts and export to CSV or JSON

SYNOPSIS
    python contact_search.py --instance-id UUID --start DATETIME --end DATETIME [OPTIONS]

DESCRIPTION
    Wraps the SearchContacts API to find contacts in a time range with optional
    filters on channel, queue, agent, initiation method, and custom contact
    attributes. Results are exported to CSV by default, or to JSON with --json.
    Due to the API's 0.5 TPS rate limit, the tool sleeps 2 seconds between pages,
    so large result sets may take a while.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --start DATETIME
        Start of time range. Format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (UTC). Required.

    --end DATETIME
        End of time range. Same format as --start. Required.

    --time-type TYPE
        Timestamp field to filter on. Default: INITIATION_TIMESTAMP.
        Choices: INITIATION_TIMESTAMP, DISCONNECT_TIMESTAMP, SCHEDULED_TIMESTAMP.

    --channel CH
        Filter by channel (repeatable). Choices: VOICE, CHAT, TASK, EMAIL.

    --queue ID
        Filter by queue ID (repeatable).

    --agent ID
        Filter by agent user ID (repeatable).

    --agent-login LOGIN
        Filter by agent login/username (repeatable). Resolved to user ID automatically.

    --initiation-method METHOD
        Filter by initiation method (repeatable).
        Choices: INBOUND, OUTBOUND, TRANSFER, CALLBACK, API,
                 QUEUE_TRANSFER, EXTERNAL_OUTBOUND, MONITOR, DISCONNECT.

    --attribute KEY=VALUE
        Filter by a custom contact attribute (repeatable).

    --sort-by FIELD
        Sort field. Default: INITIATION_TIMESTAMP.
        Choices: INITIATION_TIMESTAMP, SCHEDULED_TIMESTAMP, DISCONNECT_TIMESTAMP,
                 HANDLE_TIME, AGENT_INTERACTION_DURATION, CUSTOMER_HOLD_DURATION.

    --sort-order ORDER
        Sort direction. Default: DESCENDING. Choices: ASCENDING, DESCENDING.

    --limit N
        Maximum number of contacts to return. Default: all.

    --output FILE
        CSV output path. Default: contacts_YYYYMMDD_HHMMSS.csv.

    --json
        Emit JSON array to stdout instead of writing a CSV file.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

EXAMPLES
    # All contacts for a date range (writes contacts_YYYYMMDD_HHMMSS.csv)
    python contact_search.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-02

    # Voice inbound contacts for a specific queue
    python contact_search.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-02 \\
        --channel VOICE --initiation-method INBOUND --queue <QUEUE-ID>

    # Filter by custom attribute, write to a named file
    python contact_search.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-02 \\
        --attribute Department=Billing --output billing.csv

    # First 500 contacts oldest-first as JSON
    python contact_search.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-04 \\
        --sort-order ASCENDING --limit 500 --json | jq '.[0].Id'

IAM PERMISSIONS
    connect:SearchContacts
    connect:ListUsers

NOTES
    SearchContacts is throttled at 0.5 TPS with a burst of 1. The tool sleeps
    2 seconds between pages to stay within this limit. Large date ranges with
    many contacts may take several minutes to fetch.
"""

# SearchContacts throttle: 0.5 TPS burst 1 — sleep between pages
PAGE_SLEEP_SECS = 2.0
MAX_PAGE_SIZE   = 100

VALID_TIME_TYPES = ["INITIATION_TIMESTAMP", "DISCONNECT_TIMESTAMP", "SCHEDULED_TIMESTAMP"]

VALID_CHANNELS = ["VOICE", "CHAT", "TASK", "EMAIL"]

VALID_INITIATION_METHODS = [
    "INBOUND", "OUTBOUND", "TRANSFER", "CALLBACK", "API",
    "QUEUE_TRANSFER", "EXTERNAL_OUTBOUND", "MONITOR", "DISCONNECT",
]

VALID_SORT_FIELDS = [
    "INITIATION_TIMESTAMP", "SCHEDULED_TIMESTAMP", "DISCONNECT_TIMESTAMP",
    "HANDLE_TIME", "AGENT_INTERACTION_DURATION", "CUSTOMER_HOLD_DURATION",
]

CSV_COLUMNS = [
    "contact_id", "channel", "initiation_method",
    "initiation_timestamp", "disconnect_timestamp", "duration_seconds",
    "queue_id", "enqueue_timestamp",
    "agent_id", "connected_to_agent_timestamp",
    "customer_endpoint", "system_endpoint",
    "disconnect_reason", "initial_contact_id", "previous_contact_id",
]


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Search Amazon Connect contacts and export to CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # All contacts initiated on a date (writes contacts_YYYYMMDD_HHMMSS.csv)
  %(prog)s --instance-id <UUID> --start 2026-03-01 --end 2026-03-02

  # Voice inbound contacts for a specific queue
  %(prog)s --instance-id <UUID> --start 2026-03-01 --end 2026-03-02 \\
      --channel VOICE --initiation-method INBOUND --queue <QUEUE-ID>

  # Filter by custom contact attribute, write to a named file
  %(prog)s --instance-id <UUID> --start 2026-03-01 --end 2026-03-02 \\
      --attribute Department=Billing --output billing.csv

  # First 500 contacts, oldest-first, JSON output (pipe-friendly)
  %(prog)s --instance-id <UUID> --start 2026-03-01 --end 2026-03-04 \\
      --sort-order ASCENDING --limit 500 --json | jq '.[0].Id'

  # Using a named profile (local dev)
  %(prog)s --instance-id <UUID> --start 2026-03-01 --end 2026-03-02 --profile my-admin
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--start", required=True, metavar="DATETIME",
                   help="Start of time range (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, UTC)")
    p.add_argument("--end",   required=True, metavar="DATETIME",
                   help="End of time range (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, UTC)")
    p.add_argument("--time-type", default="INITIATION_TIMESTAMP",
                   choices=VALID_TIME_TYPES, metavar="TYPE",
                   help=(f"Timestamp field to filter on (default: INITIATION_TIMESTAMP). "
                         f"Choices: {', '.join(VALID_TIME_TYPES)}"))
    p.add_argument("--channel", action="append", dest="channels", metavar="CH",
                   choices=VALID_CHANNELS,
                   help="Filter by channel (repeatable): VOICE, CHAT, TASK, EMAIL")
    p.add_argument("--queue",  action="append", dest="queues",  metavar="ID",
                   help="Filter by queue ID (repeatable)")
    p.add_argument("--agent",  action="append", dest="agents",  metavar="ID",
                   help="Filter by agent user ID (repeatable)")
    p.add_argument("--agent-login", action="append", dest="agent_logins", metavar="LOGIN",
                   help="Filter by agent login/username (repeatable); resolved to user ID automatically")
    p.add_argument("--initiation-method", action="append", dest="initiation_methods",
                   metavar="METHOD", choices=VALID_INITIATION_METHODS,
                   help=(f"Filter by initiation method (repeatable). "
                         f"Choices: {', '.join(VALID_INITIATION_METHODS)}"))
    p.add_argument("--attribute", action="append", dest="attributes", metavar="KEY=VALUE",
                   help="Filter by contact attribute (repeatable, e.g. --attribute lang=en)")
    p.add_argument("--sort-by", default="INITIATION_TIMESTAMP",
                   choices=VALID_SORT_FIELDS, metavar="FIELD",
                   help=(f"Sort field (default: INITIATION_TIMESTAMP). "
                         f"Choices: {', '.join(VALID_SORT_FIELDS)}"))
    p.add_argument("--sort-order", default="DESCENDING",
                   choices=["ASCENDING", "DESCENDING"])
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="Maximum number of contacts to return (default: all)")
    p.add_argument("--output", default=None, metavar="FILE",
                   help="CSV output path (default: contacts_YYYYMMDD_HHMMSS.csv)")
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Emit JSON array to stdout instead of CSV")
    p.add_argument("--region",  default=None, help="AWS region (defaults to session/CloudShell region)")
    p.add_argument("--profile", default=None, help="AWS named profile")
    return p.parse_args()


# ── AWS client ────────────────────────────────────────────────────────────────

def make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


# ── Time parsing ──────────────────────────────────────────────────────────────

def parse_datetime(s: str) -> dt.datetime:
    """Accept YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS; returns UTC-aware datetime."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse {s!r}. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (UTC).")


# ── Agent login resolution ────────────────────────────────────────────────────

def resolve_agent_logins(client, instance_id: str, logins: list[str]) -> list[str]:
    """Return user IDs for a list of login names. Exits on any unresolved name."""
    username_to_id: dict[str, str] = {}
    token = None
    while True:
        kwargs = {"InstanceId": instance_id, "MaxResults": 100}
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
            username_to_id[u["Username"]] = u["Id"]
        token = resp.get("NextToken")
        if not token:
            break

    ids = []
    for login in logins:
        uid = username_to_id.get(login)
        if not uid:
            print(f"Error: agent login {login!r} not found in this instance.", file=sys.stderr)
            sys.exit(1)
        ids.append(uid)
    return ids


# ── Search criteria builder ───────────────────────────────────────────────────

def build_criteria(args) -> dict:
    """Translate CLI args into the SearchCriteria dict. Returns {} if no filters set."""
    c = {}
    if args.channels:
        c["Channels"] = args.channels
    if args.queues:
        c["QueueIds"] = args.queues
    if args.agents:
        c["AgentIds"] = args.agents
    if args.initiation_methods:
        c["InitiationMethods"] = args.initiation_methods
    if args.attributes:
        attr_criteria = []
        for kv in args.attributes:
            if "=" not in kv:
                print(f"Error: --attribute must be KEY=VALUE, got: {kv!r}", file=sys.stderr)
                sys.exit(1)
            key, _, val = kv.partition("=")
            attr_criteria.append({"Key": key.strip(), "Values": [val.strip()]})
        c["SearchableContactAttributes"] = {
            "Criteria": attr_criteria,
            "MatchType": "MATCH_ALL",
        }
    return c


# ── Search / pagination ───────────────────────────────────────────────────────

def search_contacts(client, instance_id, time_range, criteria, sort, limit) -> list:
    contacts = []
    token       = None
    total_count = None
    page        = 0

    while True:
        page += 1
        want = min(MAX_PAGE_SIZE, limit - len(contacts)) if limit else MAX_PAGE_SIZE
        if want <= 0:
            break

        kwargs = {
            "InstanceId": instance_id,
            "TimeRange":  time_range,
            "Sort":       sort,
            "MaxResults": want,
        }
        if criteria:
            kwargs["SearchCriteria"] = criteria
        if token:
            kwargs["NextToken"] = token

        try:
            resp = client.search_contacts(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            msg  = e.response["Error"]["Message"]
            print(f"\nError searching contacts [{code}]: {msg}", file=sys.stderr)
            sys.exit(1)

        contacts.extend(resp.get("Contacts", []))

        if total_count is None:
            total_count = resp.get("TotalCount", 0)

        print(
            f"  Fetched {len(contacts):,} / {total_count:,} contacts (page {page})...",
            end="\r",
            file=sys.stderr,
            flush=True,
        )

        token = resp.get("NextToken")
        if not token or (limit and len(contacts) >= limit):
            break

        time.sleep(PAGE_SLEEP_SECS)

    print(file=sys.stderr)  # clear the \r progress line
    return contacts


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _ts(val) -> str:
    if val is None:
        return ""
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(val)


def _endpoint(ep) -> str:
    if not ep:
        return ""
    addr = ep.get("Address", "")
    typ  = ep.get("Type", "")
    return f"{addr} ({typ})" if addr else ""


def contact_to_row(c: dict) -> list:
    qi      = c.get("QueueInfo") or {}
    ai      = c.get("AgentInfo") or {}
    init_ts = c.get("InitiationTimestamp")
    disc_ts = c.get("DisconnectTimestamp")
    duration = int((disc_ts - init_ts).total_seconds()) if init_ts and disc_ts else ""

    return [
        c.get("Id", ""),
        c.get("Channel", ""),
        c.get("InitiationMethod", ""),
        _ts(init_ts),
        _ts(disc_ts),
        duration,
        qi.get("Id", ""),
        _ts(qi.get("EnqueueTimestamp")),
        ai.get("Id", ""),
        _ts(ai.get("ConnectedToAgentTimestamp")),
        _endpoint(c.get("CustomerEndpoint")),
        _endpoint(c.get("SystemEndpoint")),
        c.get("DisconnectReason", ""),
        c.get("InitialContactId", ""),
        c.get("PreviousContactId", ""),
    ]


def write_csv(contacts: list, path: str) -> int:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for c in contacts:
            writer.writerow(contact_to_row(c))
    return len(contacts)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    args = parse_args()

    try:
        start = parse_datetime(args.start)
        end   = parse_datetime(args.end)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if start >= end:
        print("Error: --start must be before --end.", file=sys.stderr)
        sys.exit(1)

    limit  = args.limit if args.limit and args.limit > 0 else None
    client = make_client(args.region, args.profile)

    if args.agent_logins:
        resolved = resolve_agent_logins(client, args.instance_id, args.agent_logins)
        args.agents = (args.agents or []) + resolved

    criteria = build_criteria(args)

    time_range = {"Type": args.time_type, "StartTime": start, "EndTime": end}
    sort       = {"FieldName": args.sort_by, "Order": args.sort_order}

    print(f"Searching contacts: {args.start} → {args.end} ({args.time_type})", file=sys.stderr)
    if criteria:
        print(f"Filters: {json.dumps(criteria, default=str)}", file=sys.stderr)

    contacts = search_contacts(client, args.instance_id, time_range, criteria, sort, limit)

    if not contacts:
        print("No contacts found.", file=sys.stderr)
        return

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)
        print(json.dumps(contacts, indent=2, default=serial))
        return

    timestamp    = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    default_name = f"contacts_{timestamp}.csv"
    out_path     = (ct_snapshot.output_path("contact_search", args.output)
                    if args.output
                    else ct_snapshot.output_dir("contact_search") / default_name)
    n            = write_csv(contacts, out_path)
    print(f"Exported {n:,} contact(s) → {out_path}")


if __name__ == "__main__":
    main()
