#!/usr/bin/env python3
"""flow_usage.py — Count how often each contact flow is used.

Queries the Connect CloudWatch flow-log group with Logs Insights to
count how many contacts (or invocations) hit each flow over a time window.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import ct_config
import ct_snapshot

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

_MAN = """\
NAME
    flow_usage.py — Count how often each contact flow is used

SYNOPSIS
    python flow_usage.py --instance-id UUID [OPTIONS]

DESCRIPTION
    Queries the Connect CloudWatch flow-log group using Logs Insights to
    count how many contacts or invocations hit each flow over a time window.

    Two counting modes are available via --by:

      contacts    (default) — count_distinct(ContactId) per flow.
                  Each unique caller/session is counted once regardless of
                  how many blocks they executed in that flow.

      invocations — count unique (ContactId, ContactFlowId) pairs.
                  Counts each time a contact entered a flow, including
                  re-entries within the same call (e.g. loops or
                  transfers back to the same flow).

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --log-group NAME
        Override the auto-discovered Connect log group (/aws/connect/<alias>).

    --by contacts|invocations
        Counting mode (default: contacts).

    --flow NAME
        Filter output to flows matching a case-insensitive substring.

    --last DURATION
        Relative window ending now. Examples: 4h, 7d, 30d.
        Mutually exclusive with --start.

    --start YYYY-MM-DD[THH:MM:SS]
        Absolute window start. Mutually exclusive with --last.

    --end YYYY-MM-DD[THH:MM:SS]
        Absolute window end. Default: now. Used with --start.

    --csv FILE
        Write results to ~/.connecttools/flow_usage/<FILE> (or an explicit path).

    --json
        Print results as JSON to stdout.

EXAMPLES
    # All flows, last 7 days (default)
    python flow_usage.py --instance-id <UUID> --region us-east-1

    # Count by invocations instead
    python flow_usage.py --instance-id <UUID> --by invocations

    # Last 24 hours
    python flow_usage.py --instance-id <UUID> --last 24h

    # Specific date range
    python flow_usage.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-17

    # Filter to one flow
    python flow_usage.py --instance-id <UUID> --flow "Main IVR"

    # Export to CSV
    python flow_usage.py --instance-id <UUID> --csv usage.csv

    # JSON — pipe to jq
    python flow_usage.py --instance-id <UUID> --json | jq '.[] | select(.count > 100)'

IAM PERMISSIONS
    connect:DescribeInstance
    logs:StartQuery
    logs:GetQueryResults
    logs:StopQuery

NOTES
    Flow log retention must cover the requested time window. Connect flow logs
    default to no expiry but can be configured per instance. Flows with zero
    contacts in the window are not returned (they produce no log entries).
"""


# ── Argument parsing ────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Count how often each contact flow is used.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --by invocations
  %(prog)s --instance-id <UUID> --last 24h
  %(prog)s --instance-id <UUID> --start 2026-03-01 --end 2026-03-17
  %(prog)s --instance-id <UUID> --flow "Main IVR"
  %(prog)s --instance-id <UUID> --csv usage.csv
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--region",    default=None, help="AWS region")
    p.add_argument("--profile",   default=None, help="AWS named profile")
    p.add_argument("--log-group", default=None, metavar="NAME",
                   help="Override Connect log group (/aws/connect/<alias>)")
    p.add_argument("--by", choices=["contacts", "invocations"], default="contacts",
                   help="contacts: unique ContactIds; invocations: unique (ContactId, FlowId) pairs")
    p.add_argument("--flow", default=None, metavar="NAME",
                   help="Filter by flow name (case-insensitive substring)")
    # Time window
    tg = p.add_mutually_exclusive_group()
    tg.add_argument("--last",  default=None, metavar="DURATION",
                    help="Relative window: 4h, 7d, 30d")
    tg.add_argument("--start", default=None, metavar="YYYY-MM-DD[THH:MM:SS]",
                    help="Absolute window start")
    p.add_argument("--end", default=None, metavar="YYYY-MM-DD[THH:MM:SS]",
                   help="Absolute window end (default: now)")
    p.add_argument("--csv",  default=None, metavar="FILE", help="Write CSV to file")
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Print JSON to stdout")
    return p.parse_args()


# ── Time window ─────────────────────────────────────────────────────────────────

def parse_duration(s: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)([smhd])", s.lower().strip())
    if not m:
        print(f"Error: cannot parse duration {s!r}. Use e.g. 4h, 7d.", file=sys.stderr)
        sys.exit(1)
    n, unit = int(m.group(1)), m.group(2)
    return {"s": dt.timedelta(seconds=n), "m": dt.timedelta(minutes=n),
            "h": dt.timedelta(hours=n),   "d": dt.timedelta(days=n)}[unit]


def parse_window(args) -> tuple:
    now = dt.datetime.now(dt.timezone.utc)
    if args.start:
        try:
            start = dt.datetime.fromisoformat(args.start)
        except ValueError:
            print(f"Error: cannot parse --start {args.start!r}.", file=sys.stderr)
            sys.exit(1)
        if start.tzinfo is None:
            start = start.replace(tzinfo=dt.timezone.utc)
        if args.end:
            try:
                end = dt.datetime.fromisoformat(args.end)
            except ValueError:
                print(f"Error: cannot parse --end {args.end!r}.", file=sys.stderr)
                sys.exit(1)
            if end.tzinfo is None:
                end = end.replace(tzinfo=dt.timezone.utc)
        else:
            end = now
        return start, end
    delta = parse_duration(args.last) if args.last else dt.timedelta(days=7)
    return now - delta, now


# ── Client factory ───────────────────────────────────────────────────────────────

def make_clients(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    connect = session.client("connect", region_name=resolved, config=RETRY_CONFIG)
    logs    = session.client("logs",    region_name=resolved, config=RETRY_CONFIG)
    return connect, logs


# ── Log group discovery ──────────────────────────────────────────────────────────

def resolve_log_group(connect_client, instance_id: str, override: str | None) -> str:
    if override:
        cfg = ct_config.load()
        ct_config.set_log_group(cfg, instance_id, override)
        return override
    saved = ct_config.get_log_group(instance_id)
    if saved:
        return saved
    try:
        inst  = connect_client.describe_instance(InstanceId=instance_id)["Instance"]
        alias = inst.get("InstanceAlias") or instance_id
        return f"/aws/connect/{alias}"
    except ClientError as e:
        print(f"Error describing instance: {e.response['Error']['Message']}", file=sys.stderr)
        sys.exit(1)


# ── Logs Insights query ──────────────────────────────────────────────────────────

def build_query(by_mode: str) -> str:
    if by_mode == "contacts":
        return (
            "fields ContactFlowName, ContactId\n"
            "| filter ispresent(ContactId) and ispresent(ContactFlowName)\n"
            "| stats count_distinct(ContactId) as count by ContactFlowName\n"
            "| sort count desc\n"
            "| limit 1000"
        )
    else:  # invocations — dedup per (ContactId, ContactFlowId) then count
        return (
            "fields ContactFlowName, ContactId, ContactFlowId\n"
            "| filter ispresent(ContactId) and ispresent(ContactFlowName)\n"
            "| dedup ContactId, ContactFlowId\n"
            "| stats count(*) as count by ContactFlowName\n"
            "| sort count desc\n"
            "| limit 1000"
        )


def run_query(logs_client, log_group: str, query: str, start: dt.datetime, end: dt.datetime) -> list:
    try:
        resp = logs_client.start_query(
            logGroupName=log_group,
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=query,
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        print(f"Error starting query [{code}]: {msg}", file=sys.stderr)
        sys.exit(1)

    query_id = resp["queryId"]
    print("  Running Logs Insights query", end="", flush=True, file=sys.stderr)

    while True:
        time.sleep(1)
        try:
            result = logs_client.get_query_results(queryId=query_id)
        except ClientError as e:
            print(f"\nError polling query: {e.response['Error']['Message']}", file=sys.stderr)
            sys.exit(1)
        status = result["status"]
        print(".", end="", flush=True, file=sys.stderr)
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            print(file=sys.stderr)
            if status != "Complete":
                print(f"  Query ended with status: {status}", file=sys.stderr)
                sys.exit(1)
            return result["results"]


def parse_results(raw: list) -> list:
    rows = []
    for record in raw:
        fields = {f["field"]: f["value"] for f in record}
        name  = fields.get("ContactFlowName", "")
        count = fields.get("count", "0")
        if not name:
            continue
        rows.append({"flow": name, "count": int(count)})
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


# ── Output ───────────────────────────────────────────────────────────────────────

def _hr():
    print("  " + "─" * 72)


def print_human(rows, by_mode, start, end, instance_id):
    label = "CONTACTS" if by_mode == "contacts" else "INVOCATIONS"

    _hr()
    print(f"  FLOW USAGE   {instance_id}")
    _hr()
    fmt = "%Y-%m-%d %H:%M"
    print(f"  {start.strftime(fmt)} → {end.strftime(fmt)} UTC  ·  by {by_mode}\n")

    if not rows:
        print("  No data found for the requested window.")
        print()
        _hr()
        print()
        return

    flow_w = max((len(r["flow"]) for r in rows), default=20)
    flow_w = max(flow_w, 20)

    print(f"  {'FLOW':<{flow_w}}  {label:>12}")
    print(f"  {'─' * flow_w}  {'─' * 12}")
    for r in rows:
        print(f"  {r['flow']:<{flow_w}}  {r['count']:>12,}")

    total = sum(r["count"] for r in rows)
    print(f"\n  {len(rows)} flow(s)  ·  {total:,} total {by_mode}")
    print()
    _hr()
    print()


def write_csv(rows, path, by_mode):
    label = "contacts" if by_mode == "contacts" else "invocations"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["flow", "count"])
        w.writeheader()
        for r in rows:
            w.writerow({"flow": r["flow"], "count": r["count"]})


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args            = parse_args()
    connect, logs   = make_clients(args.region, args.profile)
    start, end      = parse_window(args)
    log_group       = resolve_log_group(connect, args.instance_id, args.log_group)

    print(f"  Log group : {log_group}", file=sys.stderr)
    fmt = "%Y-%m-%d %H:%M UTC"
    print(f"  Window    : {start.strftime(fmt)} → {end.strftime(fmt)}", file=sys.stderr)
    print(f"  Mode      : {args.by}", file=sys.stderr)

    query = build_query(args.by)
    raw   = run_query(logs, log_group, query, start, end)
    rows  = parse_results(raw)

    # Flow name filter
    if args.flow:
        needle = args.flow.lower()
        rows   = [r for r in rows if needle in r["flow"].lower()]

    if args.output_json:
        print(json.dumps(rows, indent=2))
        return

    print_human(rows, args.by, start, end, args.instance_id)

    if args.csv:
        dest = ct_snapshot.output_path("flow_usage", args.csv)
        write_csv(rows, dest, args.by)
        print(f"  Saved → {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
