#!/usr/bin/env python3
"""lambda_errors.py — Aggregate Lambda errors from CloudWatch Logs.

Searches /aws/lambda/<function-name> for error events over a time window,
classifies them by error type, and groups occurrences. Optionally also scans
Connect flow logs (/aws/connect/<alias>) to catch Lambda failures that are
recorded by Connect but may not produce a Lambda log entry.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from collections import defaultdict

import boto3

import ct_config
import ct_snapshot
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})
_MAX_DISPLAY = 15   # max occurrences shown per error type in human output

# CloudWatch filter to catch error-level Lambda log lines
_LAMBDA_ERROR_FILTER = '?ERROR ?"Task timed out" ?"errorType" ?"Traceback" ?"Exception"'

# Connect flow log: Lambda invocation entries
_CONNECT_LAMBDA_FILTER = '{ $.ContactFlowModuleType = "InvokeExternalResource" }'

# Regexes for parsing
_UUID_RE = re.compile(r'\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b')
_EXC_RE  = re.compile(r'([A-Za-z][A-Za-z0-9_]*(?:Exception|Error|Fault))[:\s]')

_MAN = """\
NAME
    lambda_errors.py — Aggregate Lambda errors from CloudWatch Logs

SYNOPSIS
    python lambda_errors.py --function NAME [OPTIONS]

DESCRIPTION
    Searches the Lambda function's CloudWatch log group (/aws/lambda/<name>)
    for error events over a time window, classifies them by error type, and
    groups occurrences.

    If --instance-id is provided, also scans the Connect flow logs
    (/aws/connect/<alias>) for Lambda invocation failures recorded by Connect.
    This catches errors that never produce a Lambda log entry (e.g. invocation
    failures, timeouts at the Connect level).

    Results from both sources are shown in separate sections.

OPTIONS
    --function NAME
        Lambda function name, name fragment, or full ARN. Required.
        The function name is extracted from an ARN automatically.

    --instance-id UUID
        Amazon Connect instance UUID. When provided, also searches Connect
        flow logs for Lambda errors for this function.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --log-group NAME
        Override the auto-derived Lambda log group (/aws/lambda/<name>).

    --connect-log-group NAME
        Override the auto-discovered Connect flow log group (/aws/connect/<alias>).

    --period PERIOD
        Named time period shortcut. Choices:
          today, yesterday, this-week, last-week, this-month, last-month.
        Mutually exclusive with --last and --start.

    --last DURATION
        Relative time window ending now. Examples: 30m, 4h, 7d.
        Mutually exclusive with --period and --start.

    --start YYYY-MM-DD[THH:MM:SS]
        Absolute window start. Mutually exclusive with --period and --last.

    --end YYYY-MM-DD[THH:MM:SS]
        Absolute window end. Default: now. Used with --start.

    --json
        Emit raw JSON with both Lambda log and Connect flow log results.

    --csv FILE
        Write per-error CSV to a file (all sources combined).

EXAMPLES
    # Lambda log errors for today
    python lambda_errors.py --function my-connect-lambda --region us-east-1

    # Also check Connect flow logs for this function
    python lambda_errors.py --function my-connect-lambda \\
        --instance-id <UUID> --period yesterday

    # Full ARN, last week
    python lambda_errors.py \\
        --function arn:aws:lambda:us-east-1:123456789012:function:my-fn \\
        --instance-id <UUID> --period last-week

    # JSON output
    python lambda_errors.py --function my-fn --instance-id <UUID> --json | jq '.connect_flow'

IAM PERMISSIONS
    logs:FilterLogEvents (on /aws/lambda/<function-name>)
    connect:DescribeInstance              (when --instance-id provided)
    logs:FilterLogEvents (on /aws/connect/<instance-alias>)  (when --instance-id provided)

NOTES
    The default time window is the last 24 hours if no period, --last, or
    --start flag is given. Up to 15 occurrences are shown per error type in
    human output; use --csv or --json to see all.
"""


# ── Argument parsing ───────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Aggregate Lambda errors from CloudWatch Logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --function my-fn --region us-east-1
  %(prog)s --function my-fn --instance-id <UUID> --period yesterday
  %(prog)s --function arn:aws:lambda:us-east-1:123:function:my-fn --period last-week
  %(prog)s --function my-fn --last 4h
  %(prog)s --function my-fn --instance-id <UUID> --start 2026-03-15 --end 2026-03-16
        """,
    )
    p.add_argument("--function",          required=True, metavar="NAME",
                   help="Lambda function name, fragment, or full ARN")
    p.add_argument("--instance-id",       default=None, metavar="UUID",
                   help="Connect instance UUID — enables Connect flow log search")
    p.add_argument("--region",            default=None, help="AWS region")
    p.add_argument("--profile",           default=None, help="AWS named profile")
    p.add_argument("--log-group",         default=None, metavar="NAME",
                   help="Override Lambda log group (/aws/lambda/<name>)")
    p.add_argument("--connect-log-group", default=None, metavar="NAME",
                   help="Override Connect flow log group (/aws/connect/<alias>)")
    # Time window — mutually exclusive
    tg = p.add_mutually_exclusive_group()
    tg.add_argument("--period", default=None,
                    choices=["today", "yesterday", "this-week", "last-week",
                             "this-month", "last-month"],
                    help="Named period shortcut")
    tg.add_argument("--last",  default=None, metavar="DURATION",
                    help="Relative window: 30m, 4h, 7d")
    tg.add_argument("--start", default=None, metavar="YYYY-MM-DD[THH:MM:SS]",
                    help="Absolute window start")
    p.add_argument("--end",    default=None, metavar="YYYY-MM-DD[THH:MM:SS]",
                   help="Absolute window end (default: now)")
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Emit raw JSON (pipe-friendly)")
    p.add_argument("--csv",  default=None, metavar="FILE",
                   help="Write per-error CSV to file")
    return p.parse_args()


# ── Duration / window parsing ──────────────────────────────────────────────────

def parse_duration(s: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)([smhd])", s.lower().strip())
    if not m:
        print(f"Error: cannot parse duration {s!r}. Use e.g. 30m, 4h, 7d.", file=sys.stderr)
        sys.exit(1)
    n, unit = int(m.group(1)), m.group(2)
    return {"s": dt.timedelta(seconds=n), "m": dt.timedelta(minutes=n),
            "h": dt.timedelta(hours=n),   "d": dt.timedelta(days=n)}[unit]


def _named_period(period: str) -> tuple:
    now   = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    if period == "today":
        return dt.datetime(today.year, today.month, today.day, tzinfo=dt.timezone.utc), now
    if period == "yesterday":
        d = today - dt.timedelta(days=1)
        return (dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc),
                dt.datetime(today.year, today.month, today.day, tzinfo=dt.timezone.utc))
    if period == "this-week":
        d = today - dt.timedelta(days=today.weekday())
        return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc), now
    if period == "last-week":
        d       = today - dt.timedelta(days=today.weekday())
        start_d = d - dt.timedelta(weeks=1)
        return (dt.datetime(start_d.year, start_d.month, start_d.day, tzinfo=dt.timezone.utc),
                dt.datetime(d.year,       d.month,       d.day,       tzinfo=dt.timezone.utc))
    if period == "this-month":
        return dt.datetime(today.year, today.month, 1, tzinfo=dt.timezone.utc), now
    if period == "last-month":
        first_this = dt.date(today.year, today.month, 1)
        last_month = first_this - dt.timedelta(days=1)
        return (dt.datetime(last_month.year, last_month.month, 1, tzinfo=dt.timezone.utc),
                dt.datetime(first_this.year, first_this.month, first_this.day, tzinfo=dt.timezone.utc))
    raise ValueError(f"Unknown period: {period!r}")


def parse_window(args) -> tuple:
    now = dt.datetime.now(dt.timezone.utc)
    if args.period:
        return _named_period(args.period)
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
    delta = parse_duration(args.last or "24h")
    return now - delta, now


# ── Client factory ─────────────────────────────────────────────────────────────

def make_clients(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    logs    = session.client("logs",    region_name=resolved, config=RETRY_CONFIG)
    connect = session.client("connect", region_name=resolved, config=RETRY_CONFIG)
    return logs, connect


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ms(ts: dt.datetime) -> int:
    return int(ts.timestamp() * 1000)


def extract_function_name(fn_arg: str) -> str:
    """Return bare function name from a full ARN, or return the arg unchanged."""
    if fn_arg.startswith("arn:aws"):
        parts = fn_arg.split(":")
        if len(parts) >= 7:
            return parts[6]
    return fn_arg


def fetch_instance_alias(connect, instance_id):
    try:
        return connect.describe_instance(InstanceId=instance_id)["Instance"]["InstanceAlias"]
    except ClientError:
        return None


def filter_log_events(logs_client, log_group, filter_pattern, start_ms, end_ms,
                      missing_ok: bool = False):
    """Paginate FilterLogEvents. If missing_ok, returns [] instead of exiting on 404."""
    events = []
    kwargs = {
        "logGroupName": log_group,
        "startTime":    start_ms,
        "endTime":      end_ms,
    }
    if filter_pattern:
        kwargs["filterPattern"] = filter_pattern
    while True:
        try:
            resp = logs_client.filter_log_events(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                if missing_ok:
                    return []
                print(f"Error: log group {log_group!r} not found.\n"
                      f"       Confirm the function name and that it has been invoked at least once.",
                      file=sys.stderr)
                sys.exit(1)
            print(f"Error querying logs [{code}]: {e.response['Error']['Message']}", file=sys.stderr)
            sys.exit(1)
        events.extend(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return events


# ── Lambda log parsing ─────────────────────────────────────────────────────────

def _extract_request_id(line: str) -> str | None:
    m = _UUID_RE.search(line)
    return m.group(1) if m else None


def _classify_error(line: str) -> tuple:
    """Return (error_type, message) from a Lambda log line."""
    stripped = line.strip()

    if "Task timed out" in line:
        return "Timeout", stripped[:200]

    if stripped.startswith("{") and "errorType" in stripped:
        try:
            obj = json.loads(stripped)
            return obj.get("errorType", "Error"), obj.get("errorMessage", stripped[:200])
        except (json.JSONDecodeError, ValueError):
            pass

    m = _EXC_RE.search(line)
    if m:
        return m.group(1), stripped[:200]

    return "Error", stripped[:200]


def parse_lambda_log_errors(cw_events: list) -> list:
    """
    Parse raw CloudWatch log events from a Lambda log group into error records.
    Source: "lambda_logs"
    """
    results = []
    for ev in cw_events:
        line    = ev["message"]
        ts      = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, tz=dt.timezone.utc)
        stripped = line.strip()

        if stripped.startswith(("START RequestId:", "END RequestId:", "REPORT RequestId:")):
            continue

        error_type, message = _classify_error(line)
        results.append({
            "source":     "lambda_logs",
            "timestamp":  ts,
            "request_id": _extract_request_id(line),
            "error_type": error_type,
            "message":    message,
            "contact_id": None,
            "flow_name":  None,
        })
    return results


# ── Connect flow log parsing ───────────────────────────────────────────────────

def parse_connect_flow_errors(cw_events: list, fn_match: str) -> list:
    """
    Parse Connect flow log events for Lambda invocation failures.
    Only returns entries for the target function that have an Error field.
    Source: "connect_flow_logs"
    """
    fn_match_lower = fn_match.lower()
    results = []

    for ev in cw_events:
        try:
            msg = json.loads(ev["message"].strip())
        except (json.JSONDecodeError, ValueError):
            continue

        if msg.get("ContactFlowModuleType") not in ("InvokeExternalResource", "InvokeLambdaFunction"):
            continue

        params = msg.get("Parameters", {})
        arn    = params.get("FunctionArn") or params.get("LambdaFunctionARN") or ""
        if not arn or fn_match_lower not in arn.lower():
            continue

        error = msg.get("Error")
        if not error:
            continue   # successful invocation — skip

        ts_str = msg.get("Timestamp")
        if ts_str:
            try:
                ts = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                ts = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, tz=dt.timezone.utc)
        else:
            ts = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, tz=dt.timezone.utc)

        results.append({
            "source":     "connect_flow_logs",
            "timestamp":  ts,
            "request_id": None,
            "error_type": error,
            "message":    msg.get("Results") or "",
            "contact_id": msg.get("ContactId") or "",
            "flow_name":  msg.get("ContactFlowName") or "",
        })

    return results


# ── Aggregation ────────────────────────────────────────────────────────────────

def aggregate(errors: list) -> dict:
    by_type: dict = defaultdict(list)
    for err in errors:
        by_type[err["error_type"]].append(err)
    return {
        "total":   len(errors),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: len(kv[1]), reverse=True)),
    }


# ── Human-readable output ──────────────────────────────────────────────────────

def _hr():
    print("  " + "─" * 68)


def _pct(n, total):
    return f"{n / total * 100:.1f}%" if total else "—"


def _print_section(title, agg, show_contact_ids: bool):
    total = agg["total"]
    print(f"\n  ── {title} ──")
    if total == 0:
        print("     No errors found.")
        return

    print(f"     {total} error event(s)  ·  {len(agg['by_type'])} type(s)")

    for error_type, errs in agg["by_type"].items():
        count = len(errs)
        print(f"\n     {error_type}  ({count} occurrence(s))")
        shown = errs[:_MAX_DISPLAY]
        for err in shown:
            ts_str = err["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            if show_contact_ids and err.get("contact_id"):
                cid      = err["contact_id"]
                flow_str = f"  [{err['flow_name']}]" if err.get("flow_name") else ""
                print(f"       {ts_str} UTC   {cid}{flow_str}")
            else:
                rid = f"  [{err['request_id'][:8]}…]" if err.get("request_id") else ""
                msg = err.get("message", "")
                if len(msg) > 100:
                    msg = msg[:99] + "…"
                print(f"       {ts_str} UTC{rid}")
                if msg and msg != error_type:
                    print(f"         \033[90m{msg}\033[0m")
        if count > _MAX_DISPLAY:
            print(f"       \033[90m… {count - _MAX_DISPLAY} more — use --csv or --json for full list\033[0m")


def print_human(fn_name, lambda_agg, connect_agg, start_dt, end_dt,
                lambda_log_group, connect_log_group):
    _hr()
    print(f"  LAMBDA ERROR REPORT   {fn_name}")
    _hr()
    print(f"  Window   : {start_dt.strftime('%Y-%m-%d %H:%M')} UTC"
          f"  →  {end_dt.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Lambda log group : {lambda_log_group}")
    if connect_log_group:
        print(f"  Connect log group: {connect_log_group}")
    print()

    total = lambda_agg["total"] + (connect_agg["total"] if connect_agg else 0)
    if total == 0:
        print("  No errors found in this window.")
        _hr()
        print()
        return

    _print_section("Lambda log errors", lambda_agg, show_contact_ids=False)
    if connect_agg is not None:
        _print_section("Connect flow log errors  (with contact IDs)", connect_agg,
                       show_contact_ids=True)

    print()
    _hr()
    print()


# ── CSV output ─────────────────────────────────────────────────────────────────

def write_csv(errors: list, path: str):
    fields = ["source", "timestamp", "error_type", "request_id",
              "contact_id", "flow_name", "message"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for err in errors:
            row = dict(err)
            row["timestamp"] = err["timestamp"].isoformat()
            w.writerow(row)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    args = parse_args()
    logs_client, connect_client = make_clients(args.region, args.profile)
    start_dt, end_dt            = parse_window(args)

    fn_name         = extract_function_name(args.function)
    lambda_log_group = args.log_group or f"/aws/lambda/{fn_name}"

    # ── Lambda log search ──────────────────────────────────────────────────────
    print(f"  Lambda log group : {lambda_log_group}", file=sys.stderr)
    print(f"  Window    : {start_dt.strftime('%Y-%m-%d %H:%M')} UTC"
          f"  →  {end_dt.strftime('%Y-%m-%d %H:%M')} UTC", file=sys.stderr)
    print(f"  Fetching Lambda error logs...", file=sys.stderr)

    lambda_events = filter_log_events(
        logs_client, lambda_log_group, _LAMBDA_ERROR_FILTER,
        _ms(start_dt), _ms(end_dt),
    )
    print(f"  {len(lambda_events)} matching Lambda log event(s). Parsing...", file=sys.stderr)
    lambda_errors = parse_lambda_log_errors(lambda_events)
    lambda_agg    = aggregate(lambda_errors)

    # ── Connect flow log search (optional) ────────────────────────────────────
    connect_errors     = []
    connect_agg        = None
    connect_log_group  = None

    if args.instance_id:
        connect_log_group = args.connect_log_group
        if connect_log_group:
            cfg = ct_config.load()
            ct_config.set_log_group(cfg, args.instance_id, connect_log_group)
        else:
            connect_log_group = ct_config.get_log_group(args.instance_id)
        if not connect_log_group:
            alias = fetch_instance_alias(connect_client, args.instance_id)
            if alias:
                connect_log_group = f"/aws/connect/{alias}"
            else:
                print("  Warning: could not resolve Connect instance alias. "
                      "Pass --connect-log-group to search flow logs.",
                      file=sys.stderr)

        if connect_log_group:
            print(f"  Connect log group: {connect_log_group}", file=sys.stderr)
            print(f"  Fetching Connect flow logs for Lambda invocations...", file=sys.stderr)
            connect_events = filter_log_events(
                logs_client, connect_log_group, _CONNECT_LAMBDA_FILTER,
                _ms(start_dt), _ms(end_dt), missing_ok=True,
            )
            print(f"  {len(connect_events)} Connect flow log event(s). Parsing...", file=sys.stderr)
            connect_errors = parse_connect_flow_errors(connect_events, args.function)
            connect_agg    = aggregate(connect_errors)

    # ── Output ────────────────────────────────────────────────────────────────
    all_errors = lambda_errors + connect_errors

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)

        doc = {
            "function":        fn_name,
            "lambda_log_group": lambda_log_group,
            "connect_log_group": connect_log_group,
            "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "lambda_logs": {
                "total":   lambda_agg["total"],
                "by_type": {
                    etype: [
                        {k: v.isoformat() if hasattr(v, "isoformat") else v
                         for k, v in err.items()}
                        for err in errs
                    ]
                    for etype, errs in lambda_agg["by_type"].items()
                },
            },
            "connect_flow": None if connect_agg is None else {
                "total":   connect_agg["total"],
                "by_type": {
                    etype: [
                        {k: v.isoformat() if hasattr(v, "isoformat") else v
                         for k, v in err.items()}
                        for err in errs
                    ]
                    for etype, errs in connect_agg["by_type"].items()
                },
            },
        }
        print(json.dumps(doc, indent=2, default=serial))
    else:
        print_human(fn_name, lambda_agg, connect_agg,
                    start_dt, end_dt, lambda_log_group, connect_log_group)

    if args.csv:
        csv_path = ct_snapshot.output_path("lambda_errors", args.csv)
        write_csv(all_errors, csv_path)
        print(f"  Saved → {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
