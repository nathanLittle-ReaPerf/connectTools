#!/usr/bin/env python3
"""agent_activity.py — Per-agent activity report for a given period (GetMetricDataV2)."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

METRICS = [
    "CONTACTS_HANDLED",
    "AGENT_OCCUPANCY",
    "SUM_ONLINE_TIME_AGENT",
    "SUM_CONTACT_TIME_AGENT",
    "SUM_IDLE_TIME_AGENT",
    "SUM_NON_PRODUCTIVE_TIME_AGENT",
    "SUM_ERROR_STATUS_TIME_AGENT",
    "AVG_HANDLE_TIME",
    "AVG_AFTER_CONTACT_WORK_TIME",
    "AVG_TALK_TIME",
]

CSV_COLUMNS = [
    "AgentUsername",
    "AgentId",
    "ContactsHandled",
    "Occupancy_pct",
    "OnlineTime_sec",
    "OnContactTime_sec",
    "IdleTime_sec",
    "NonProductiveTime_sec",
    "ErrorStatusTime_sec",
    "AvgHandleTime_sec",
    "AvgACW_sec",
    "AvgTalkTime_sec",
]

NAMED_PERIODS = ["today", "yesterday", "this-week", "last-week", "this-month", "last-month"]


# ── Date helpers ───────────────────────────────────────────────────────────────

def resolve_period(period: str, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Return (start, end) UTC datetimes for a named period."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today, now
    if period == "yesterday":
        start = today - dt.timedelta(days=1)
        return start, today
    if period == "this-week":
        start = today - dt.timedelta(days=today.weekday())  # Monday
        return start, now
    if period == "last-week":
        this_monday = today - dt.timedelta(days=today.weekday())
        start = this_monday - dt.timedelta(weeks=1)
        return start, this_monday
    if period == "this-month":
        return today.replace(day=1), now
    if period == "last-month":
        first_this = today.replace(day=1)
        if first_this.month == 1:
            start = first_this.replace(year=first_this.year - 1, month=12)
        else:
            start = first_this.replace(month=first_this.month - 1)
        return start, first_this
    raise ValueError(f"Unknown period: {period!r}")


def parse_date(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)


# ── AWS helpers ────────────────────────────────────────────────────────────────

def make_client(region: str | None, profile: str | None):
    session = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


def resolve_instance(client, instance_id: str | None, instance_arn: str | None) -> tuple[str, str]:
    if instance_arn:
        m = re.search(r"instance/([a-z0-9-]+)$", instance_arn)
        iid = m.group(1) if m else instance_arn
        return iid, instance_arn
    try:
        resp = client.describe_instance(InstanceId=instance_id)
        arn = resp["Instance"]["Arn"]
        return instance_id, arn
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        print(f"Error resolving instance [{code}]: {msg}", file=sys.stderr)
        sys.exit(1)


def list_users(client, instance_id: str) -> dict[str, str]:
    """Return {user_id: username} for all users in the instance."""
    users: dict[str, str] = {}
    token = None
    while True:
        kwargs: dict = {"InstanceId": instance_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_users(**kwargs)
        except ClientError as e:
            print(f"Warning: could not list users: {e}", file=sys.stderr)
            return users
        for u in resp.get("UserSummaryList", []):
            users[u["Id"]] = u["Username"]
        token = resp.get("NextToken")
        if not token:
            return users


def list_routing_profile_ids(client, instance_id: str) -> list[str]:
    ids: list[str] = []
    token = None
    while True:
        kwargs: dict = {"InstanceId": instance_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_routing_profiles(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            msg  = e.response["Error"]["Message"]
            print(f"Error listing routing profiles [{code}]: {msg}", file=sys.stderr)
            sys.exit(1)
        ids.extend(rp["Id"] for rp in resp.get("RoutingProfileSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return ids


def _chunks(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def agent_id_from_dim(value: str) -> str:
    """Extract bare agent ID from ARN or return value as-is."""
    m = re.search(r"/agent/([a-z0-9-]+)$", value)
    return m.group(1) if m else value


def get_agent_metrics(
    client,
    resource_arn: str,
    start: dt.datetime,
    end: dt.datetime,
    filter_key: str,
    filter_values: list[str],
) -> dict[str, dict[str, float]]:
    """Return {agent_id: {metric_name: value}} for the given filter."""
    results: dict[str, dict[str, float]] = {}

    for batch in _chunks(filter_values, 100):
        token = None
        while True:
            kwargs: dict = {
                "ResourceArn": resource_arn,
                "StartTime":   start,
                "EndTime":     end,
                "Interval":    {"IntervalPeriod": "TOTAL"},
                "Filters":     [{"FilterKey": filter_key, "FilterValues": batch}],
                "Groupings":   ["AGENT"],
                "Metrics":     [{"Name": m} for m in METRICS],
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
                agent_id = agent_id_from_dim(mr.get("Dimensions", {}).get("AGENT", ""))
                row = results.setdefault(agent_id, {})
                for c in mr.get("Collections", []):
                    name = c.get("Metric", {}).get("Name")
                    if name:
                        # Sum across batches; for averages this over-counts but
                        # in practice each agent belongs to one routing profile
                        row[name] = row.get(name, 0.0) + (c.get("Value") or 0.0)
            token = resp.get("NextToken")
            if not token:
                break

    return results


# ── CSV output ─────────────────────────────────────────────────────────────────

def write_csv(path: Path, agent_metrics: dict, user_map: dict) -> int:
    rows = []
    for agent_id, m in sorted(agent_metrics.items(),
                               key=lambda kv: user_map.get(kv[0], kv[0]).lower()):
        rows.append({
            "AgentUsername":       user_map.get(agent_id, agent_id),
            "AgentId":             agent_id,
            "ContactsHandled":     int(round(m.get("CONTACTS_HANDLED", 0))),
            "Occupancy_pct":       round(m.get("AGENT_OCCUPANCY", 0), 1),
            "OnlineTime_sec":      int(round(m.get("SUM_ONLINE_TIME_AGENT", 0))),
            "OnContactTime_sec":   int(round(m.get("SUM_CONTACT_TIME_AGENT", 0))),
            "IdleTime_sec":        int(round(m.get("SUM_IDLE_TIME_AGENT", 0))),
            "NonProductiveTime_sec": int(round(m.get("SUM_NON_PRODUCTIVE_TIME_AGENT", 0))),
            "ErrorStatusTime_sec": int(round(m.get("SUM_ERROR_STATUS_TIME_AGENT", 0))),
            "AvgHandleTime_sec":   int(round(m.get("AVG_HANDLE_TIME", 0))),
            "AvgACW_sec":          int(round(m.get("AVG_AFTER_CONTACT_WORK_TIME", 0))),
            "AvgTalkTime_sec":     int(round(m.get("AVG_TALK_TIME", 0))),
        })
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Per-agent activity report for a given period.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --period last-month
  %(prog)s --instance-id <UUID> --period this-week --agent jsmith
  %(prog)s --instance-id <UUID> --period this-week --agent jsmith --agent bjones
  %(prog)s --instance-id <UUID> --start 2025-01-01 --end 2025-01-31
  %(prog)s --instance-id <UUID> --period yesterday --output /tmp/report.csv
        """,
    )
    inst = p.add_mutually_exclusive_group(required=True)
    inst.add_argument("--instance-id",  metavar="UUID")
    inst.add_argument("--instance-arn", metavar="ARN")

    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--period", choices=NAMED_PERIODS,
                      metavar="PERIOD", help=f"Named period: {', '.join(NAMED_PERIODS)}")
    when.add_argument("--start", metavar="YYYY-MM-DD", help="Custom range start (inclusive)")

    p.add_argument("--agent",   metavar="LOGIN", action="append", default=None,
                   help="Filter to a specific agent login (repeatable)")
    p.add_argument("--end",     metavar="YYYY-MM-DD",
                   help="Custom range end (inclusive; defaults to today if --start given)")
    p.add_argument("--region",  default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--output",  default=None, metavar="PATH",
                   help="CSV output path (default: auto-named in current directory)")
    args = p.parse_args()

    now_utc = dt.datetime.now(dt.timezone.utc)

    if args.period:
        start, end = resolve_period(args.period, now_utc)
        period_label = args.period
    else:
        start = parse_date(args.start)
        end_date = args.end or now_utc.strftime("%Y-%m-%d")
        end = parse_date(end_date) + dt.timedelta(days=1)  # end is inclusive
        period_label = f"{args.start}_to_{end_date}"

    if (now_utc - start).days > 93:
        print(
            "Error: start date is outside Amazon Connect's ~3-month metrics retention window.\n"
            f"Earliest queryable date is approximately "
            f"{(now_utc - dt.timedelta(days=93)).strftime('%Y-%m-%d')}.",
            file=sys.stderr,
        )
        sys.exit(1)

    out_path = Path(args.output) if args.output else \
        Path(f"agent_activity_{period_label}_{now_utc.strftime('%Y-%m-%d')}.csv")

    client = make_client(args.region, args.profile)
    instance_id, instance_arn = resolve_instance(client, args.instance_id, args.instance_arn)

    print(f"Fetching agents...", end=" ", flush=True)
    user_map = list_users(client, instance_id)
    print(f"{len(user_map)} found.")

    if args.agent:
        # Resolve each login to a user ID
        username_to_id = {v: k for k, v in user_map.items()}
        agent_ids = []
        for login in args.agent:
            uid = username_to_id.get(login)
            if not uid:
                print(f"Error: agent login {login!r} not found in this instance.", file=sys.stderr)
                sys.exit(1)
            agent_ids.append(uid)
        filter_key, filter_values = "AGENT", agent_ids
    else:
        print(f"Fetching routing profiles...", end=" ", flush=True)
        filter_values = list_routing_profile_ids(client, instance_id)
        print(f"{len(filter_values)} found.")
        filter_key = "ROUTING_PROFILE"

    print(f"Fetching metrics ({start:%Y-%m-%d} to {(end - dt.timedelta(seconds=1)):%Y-%m-%d})...",
          end=" ", flush=True)
    agent_metrics = get_agent_metrics(client, instance_arn, start, end, filter_key, filter_values)
    print(f"{len(agent_metrics)} agents with activity.")

    count = write_csv(out_path, agent_metrics, user_map)
    print(f"Wrote {count} rows -> {out_path}")


if __name__ == "__main__":
    main()
