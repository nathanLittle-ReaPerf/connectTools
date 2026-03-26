#!/usr/bin/env python3
"""contacts-handled: Sum 'Contacts Handled' for the previous calendar month across an Amazon Connect instance."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

_MAN = """\
NAME
    contacts_handled.py — Sum CONTACTS_HANDLED for a calendar month across an instance

SYNOPSIS
    python contacts_handled.py --instance-id UUID [OPTIONS]
    python contacts_handled.py --instance-arn ARN  [OPTIONS]

DESCRIPTION
    Sums the CONTACTS_HANDLED metric across all queues in an Amazon Connect
    instance for the previous calendar month (or a specified month). Uses
    GetMetricDataV2, discovers all queue IDs automatically, and batches them
    in groups of 100 to stay within API limits. Historical data is available
    for approximately 3 months; requests outside that window exit with an error.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Mutually exclusive with --instance-arn.

    --instance-arn ARN
        Amazon Connect instance ARN. Mutually exclusive with --instance-id.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --timezone TZ
        Timezone for the aggregation window. Default: UTC.
        Example: America/Chicago.

    --month YYYY-MM
        Month to report. Default: previous calendar month.

EXAMPLES
    # Previous month using instance UUID
    python contacts_handled.py --instance-id <UUID>

    # Using an instance ARN
    python contacts_handled.py --instance-arn arn:aws:connect:us-west-2:123456789:instance/<UUID>

    # Specific month, non-UTC timezone
    python contacts_handled.py --instance-id <UUID> --region us-west-2 \\
        --timezone America/Chicago --month 2026-02

IAM PERMISSIONS
    connect:DescribeInstance
    connect:ListQueues
    connect:GetMetricDataV2

NOTES
    GetMetricDataV2 retains historical data for approximately 93 days. Requesting
    a month older than that will exit with an error and show the earliest queryable
    month. The metric is summed over all STANDARD queues; contacts with no
    queue attribution (e.g. direct inbound to agent) are not captured by
    GetMetricDataV2 and are excluded.
"""


# ── Date helpers ──────────────────────────────────────────────────────────────

def month_range(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    """Return (start, end) UTC datetimes bracketing the given calendar month."""
    start = dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
    if month == 12:
        end = dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc)
    else:
        end = dt.datetime(year, month + 1, 1, tzinfo=dt.timezone.utc)
    return start, end


def prev_month(now_utc: dt.datetime) -> tuple[int, int]:
    """Return (year, month) of the previous calendar month."""
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


def instance_id_from_arn(arn: str) -> str:
    m = re.search(r"instance/([a-z0-9-]+)$", arn)
    if not m:
        raise ValueError(f"Could not parse instance ID from ARN: {arn!r}")
    return m.group(1)


def resolve_instance(client, instance_id: str | None, instance_arn: str | None) -> tuple[str, str]:
    """
    Return (instance_id, instance_arn).
    If only --instance-id is given, calls DescribeInstance to get the full ARN.
    """
    if instance_arn:
        return instance_id_from_arn(instance_arn), instance_arn
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


def _chunks(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def get_contacts_handled(
    client, resource_arn: str, start: dt.datetime, end: dt.datetime,
    queue_ids: list[str], tz: str = "UTC",
) -> int:
    total = 0
    for batch in _chunks(queue_ids, 100):
        token = None
        while True:
            kwargs = {
                "ResourceArn": resource_arn,
                "StartTime":   start,
                "EndTime":     end,
                "Interval":    {"IntervalPeriod": "TOTAL", "TimeZone": tz},
                "Filters":     [{"FilterKey": "QUEUE", "FilterValues": batch}],
                "Metrics":     [{"Name": "CONTACTS_HANDLED"}],
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
            for mr in resp.get("MetricResults", []):
                for c in mr.get("Collections", []):
                    if c.get("Metric", {}).get("Name") == "CONTACTS_HANDLED":
                        total += int(round(c.get("Value", 0) or 0))
            token = resp.get("NextToken")
            if not token:
                break
    return total


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    p = argparse.ArgumentParser(
        description="Sum Amazon Connect Contacts Handled for the previous calendar month.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID>
  %(prog)s --instance-arn arn:aws:connect:us-west-2:123456789:instance/<UUID>
  %(prog)s --instance-id <UUID> --region us-west-2 --timezone America/Chicago
        """,
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--instance-id",  metavar="UUID", help="Connect instance UUID")
    group.add_argument("--instance-arn", metavar="ARN",  help="Connect instance ARN")
    p.add_argument("--region",   default=None, help="AWS region (defaults to session/CloudShell region)")
    p.add_argument("--profile",  default=None, help="AWS named profile")
    p.add_argument("--timezone", default="UTC", metavar="TZ",
                   help="Timezone for aggregation window (default: UTC)")
    p.add_argument("--month", default=None, metavar="YYYY-MM",
                   help="Month to report (e.g. 2025-01); defaults to previous calendar month")
    args = p.parse_args()

    if args.month:
        try:
            parsed = dt.datetime.strptime(args.month, "%Y-%m")
            year, month = parsed.year, parsed.month
        except ValueError:
            print(f"Error: --month must be in YYYY-MM format, got '{args.month}'", file=sys.stderr)
            sys.exit(1)
    else:
        year, month = prev_month(dt.datetime.now(dt.timezone.utc))

    client = make_client(args.region, args.profile)
    instance_id, instance_arn = resolve_instance(client, args.instance_id, args.instance_arn)

    standard_queue_ids = list_queue_ids_by_type(client, instance_id, "STANDARD")
    if not standard_queue_ids:
        print("No queues found in the instance — nothing to aggregate.")
        return

    start, end = month_range(year, month)

    now_utc = dt.datetime.now(dt.timezone.utc)

    if start > now_utc:
        print(f"Error: {year}-{month:02d} is in the future.", file=sys.stderr)
        sys.exit(1)

    # Cap end at now for the current (in-progress) month
    if end > now_utc:
        end = now_utc

    # GetMetricDataV2 retains historical data for approximately 3 months.
    if (now_utc - start).days > 93:
        print(
            f"Error: {year}-{month:02d} is outside Amazon Connect's ~3-month metrics retention window.\n"
            f"Earliest queryable month is approximately "
            f"{(now_utc - dt.timedelta(days=93)).strftime('%Y-%m')}.",
            file=sys.stderr,
        )
        sys.exit(1)

    total = get_contacts_handled(client, instance_arn, start, end, standard_queue_ids, tz=args.timezone)

    print(f"{start:%Y-%m-%d} to {end:%Y-%m-%d} ({args.timezone}): {total:,} Contacts Handled")


if __name__ == "__main__":
    main()
