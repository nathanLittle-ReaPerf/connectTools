#!/usr/bin/env python3
"""agent_contacts: CONTACTS_HANDLED per agent for a calendar month, broken down by queue type."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

_MAN = """\
NAME
    agent_contacts.py — CONTACTS_HANDLED per agent for a calendar month

SYNOPSIS
    python agent_contacts.py --instance-id UUID [OPTIONS]

DESCRIPTION
    Reports CONTACTS_HANDLED broken down by agent login ID for a given calendar
    month. Each row shows contacts handled via standard queues, agent (personal)
    queues, and a combined total. Results are sorted by total descending.
    Uses GetMetricDataV2 and discovers all queue IDs automatically.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --timezone TZ
        Timezone for the aggregation window. Default: UTC.
        Example: America/Chicago.

    --month YYYY-MM
        Month to report. Default: previous calendar month.

    --csv FILE
        Write results to a CSV file.

    --json
        Print results as JSON to stdout.

EXAMPLES
    python agent_contacts.py --instance-id <UUID>
    python agent_contacts.py --instance-id <UUID> --month 2026-01
    python agent_contacts.py --instance-id <UUID> --csv agents.csv
    python agent_contacts.py --instance-id <UUID> --json | jq '.agents[] | select(.total > 100)'

IAM PERMISSIONS
    connect:DescribeInstance
    connect:ListQueues
    connect:ListUsers
    connect:GetMetricDataV2

NOTES
    GetMetricDataV2 retains historical data for approximately 93 days. Requesting
    a month older than that will exit with an error and show the earliest queryable
    month.
"""


# ── Date helpers ──────────────────────────────────────────────────────────────

def month_range(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
    if month == 12:
        end = dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc)
    else:
        end = dt.datetime(year, month + 1, 1, tzinfo=dt.timezone.utc)
    return start, end


def prev_month(now_utc: dt.datetime) -> tuple[int, int]:
    if now_utc.month == 1:
        return now_utc.year - 1, 12
    return now_utc.year, now_utc.month - 1


# ── AWS helpers ───────────────────────────────────────────────────────────────

def make_client(region, profile):
    session = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


def resolve_instance(client, instance_id: str) -> tuple[str, str]:
    try:
        resp = client.describe_instance(InstanceId=instance_id)
        arn = resp["Instance"]["Arn"]
        return instance_id, arn
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        print(f"Error resolving instance [{code}]: {msg}", file=sys.stderr)
        sys.exit(1)


def list_queue_ids_by_type(client, instance_id: str, queue_type: str) -> list[str]:
    queue_ids, token = [], None
    while True:
        kwargs = {"InstanceId": instance_id, "QueueTypes": [queue_type], "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_queues(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            msg  = e.response["Error"]["Message"]
            print(f"Error listing queues [{code}]: {msg}", file=sys.stderr)
            sys.exit(1)
        queue_ids.extend(q["Id"] for q in resp.get("QueueSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return queue_ids


def list_users(client, instance_id: str) -> dict[str, str]:
    """Returns {user_id: username (login ID)}."""
    users: dict[str, str] = {}
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
            users[u["Id"]] = u["Username"]
        token = resp.get("NextToken")
        if not token:
            return users


def _chunks(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def _accumulate_agent_metric(resp: dict, agent_counts: dict[str, int]) -> None:
    for mr in resp.get("MetricResults", []):
        agent_id = mr.get("Dimensions", {}).get("AGENT")
        if not agent_id:
            continue
        for c in mr.get("Collections", []):
            if c.get("Metric", {}).get("Name") == "CONTACTS_HANDLED_BY_CONNECTED_TO_AGENT":
                count = int(round(c.get("Value", 0) or 0))
                agent_counts[agent_id] = agent_counts.get(agent_id, 0) + count


def get_total_handled_by_agent(
    client, resource_arn: str, start: dt.datetime, end: dt.datetime, tz: str = "UTC",
) -> dict[str, int]:
    """Returns {agent_id: contacts_handled} across all queues (no queue filter)."""
    agent_counts: dict[str, int] = {}
    token = None
    while True:
        kwargs = {
            "ResourceArn": resource_arn,
            "StartTime":   start,
            "EndTime":     end,
            "Interval":    {"IntervalPeriod": "TOTAL", "TimeZone": tz},
            "Groupings":   ["AGENT"],
            "Metrics":     [{"Name": "CONTACTS_HANDLED_BY_CONNECTED_TO_AGENT"}],
        }
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.get_metric_data_v2(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            msg  = e.response["Error"]["Message"]
            print(f"Error fetching metrics [{code}]: {msg}", file=sys.stderr)
            sys.exit(1)
        _accumulate_agent_metric(resp, agent_counts)
        token = resp.get("NextToken")
        if not token:
            break
    return agent_counts


# ── Data assembly ─────────────────────────────────────────────────────────────

def build_rows(total_by_agent: dict[str, int], user_map: dict[str, str]) -> list[dict]:
    rows = [
        {"login_id": user_map.get(agent_id, agent_id), "total": total}
        for agent_id, total in total_by_agent.items()
    ]
    rows.sort(key=lambda r: (-r["total"], r["login_id"]))
    return rows


# ── Output ────────────────────────────────────────────────────────────────────

def print_table(rows: list[dict], period: str, timezone: str) -> None:
    if not rows:
        print(f"{period} ({timezone}): no contacts handled.")
        return

    col_login = max(max(len(r["login_id"]) for r in rows), len("Login ID"))
    col_num   = max(max(len(f"{r['total']:,}") for r in rows), len("Handled"))
    sep       = f"{'-' * col_login}   {'-' * col_num}"

    print(f"{period} ({timezone})")
    print()
    print(f"{'Login ID':<{col_login}}   {'Handled':>{col_num}}")
    print(sep)
    for r in rows:
        print(f"{r['login_id']:<{col_login}}   {r['total']:>{col_num},}")
    print(sep)
    print(f"{'Total':<{col_login}}   {sum(r['total'] for r in rows):>{col_num},}")


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["login_id", "total"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV written to {path}", file=sys.stderr)


def print_json(rows: list[dict], start: dt.datetime, end: dt.datetime, timezone: str) -> None:
    out = {
        "period":   f"{start:%Y-%m-%d} to {end:%Y-%m-%d}",
        "timezone": timezone,
        "agents":   rows,
        "summary": {
            "total": sum(r["total"] for r in rows),
        },
    }
    print(json.dumps(out, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    p = argparse.ArgumentParser(
        description="CONTACTS_HANDLED per agent for a calendar month.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID>
  %(prog)s --instance-id <UUID> --month 2026-01
  %(prog)s --instance-id <UUID> --csv agents.csv
  %(prog)s --instance-id <UUID> --json | jq '.agents[] | select(.total > 100)'
        """,
    )
    p.add_argument("--instance-id", metavar="UUID", required=True, help="Connect instance UUID")
    p.add_argument("--region",      default=None,  help="AWS region")
    p.add_argument("--profile",     default=None,  help="AWS named profile")
    p.add_argument("--timezone",    default="UTC", metavar="TZ", help="Timezone (default: UTC)")
    p.add_argument("--month",       default=None,  metavar="YYYY-MM", help="Month to report (default: previous month)")
    p.add_argument("--csv",         default=None,  metavar="FILE", help="Write results to CSV")
    p.add_argument("--json",        action="store_true", help="Print results as JSON")
    args = p.parse_args()

    if args.month:
        try:
            parsed = dt.datetime.strptime(args.month, "%Y-%m")
            year, month = parsed.year, parsed.month
        except ValueError:
            print(f"Error: --month must be YYYY-MM, got '{args.month}'", file=sys.stderr)
            sys.exit(1)
    else:
        year, month = prev_month(dt.datetime.now(dt.timezone.utc))

    client = make_client(args.region, args.profile)
    instance_id, instance_arn = resolve_instance(client, args.instance_id)

    user_map = list_users(client, instance_id)

    start, end = month_range(year, month)
    now_utc = dt.datetime.now(dt.timezone.utc)

    if start > now_utc:
        print(f"Error: {year}-{month:02d} is in the future.", file=sys.stderr)
        sys.exit(1)

    if end > now_utc:
        end = now_utc

    if (now_utc - start).days > 93:
        print(
            f"Error: {year}-{month:02d} is outside Amazon Connect's ~3-month metrics retention window.\n"
            f"Earliest queryable month is approximately "
            f"{(now_utc - dt.timedelta(days=93)).strftime('%Y-%m')}.",
            file=sys.stderr,
        )
        sys.exit(1)

    total_by_agent = get_total_handled_by_agent(client, instance_arn, start, end, tz=args.timezone)

    rows = build_rows(total_by_agent, user_map)

    if args.json:
        print_json(rows, start, end, args.timezone)
    else:
        print_table(rows, f"{start:%Y-%m-%d} to {end:%Y-%m-%d}", args.timezone)

    if args.csv:
        write_csv(rows, args.csv)


if __name__ == "__main__":
    main()
