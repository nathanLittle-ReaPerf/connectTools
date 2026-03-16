#!/usr/bin/env python3
"""lambda_errors.py — Aggregate Lambda errors across contacts for a given function.

Scans Connect flow logs over a time window, finds every invocation of the
specified Lambda function, and groups results by error type — showing which
contacts were affected and how many.
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
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG         = Config(retries={"max_attempts": 5, "mode": "adaptive"})
_LAMBDA_MODULE_TYPES = {"InvokeExternalResource", "InvokeLambdaFunction"}
_MAX_DISPLAY         = 15   # max contact IDs shown per error group in human output


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Aggregate Lambda invocation errors across contacts for a given function.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --function my-auth-function --region us-east-1
  %(prog)s --instance-id <UUID> --function my-auth-function --period yesterday
  %(prog)s --instance-id <UUID> --function my-auth-function --period last-week
  %(prog)s --instance-id <UUID> --function my-auth-function --last 4h
  %(prog)s --instance-id <UUID> --function my-auth-function --start 2026-03-15 --end 2026-03-16
  %(prog)s --instance-id <UUID> --function my-auth-function --csv errors.csv
  %(prog)s --instance-id <UUID> --function my-auth-function --json | jq '.errors'
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--function",    required=True, metavar="NAME",
                   help="Lambda function name or ARN fragment to match")
    p.add_argument("--region",    default=None,  help="AWS region")
    p.add_argument("--profile",   default=None,  help="AWS named profile")
    p.add_argument("--log-group", default=None,  metavar="NAME",
                   help="Override auto-discovered Connect log group")
    # Time window — mutually exclusive
    tg = p.add_mutually_exclusive_group()
    tg.add_argument("--period", default=None,
                    choices=["today", "yesterday", "this-week", "last-week",
                             "this-month", "last-month"],
                    help="Named period shortcut")
    tg.add_argument("--last",  default=None, metavar="DURATION",
                    help="Relative window: 30m, 4h, 7d")
    tg.add_argument("--start", default=None, metavar="YYYY-MM-DD[THH:MM:SS]",
                    help="Absolute window start (requires --end)")
    p.add_argument("--end",    default=None, metavar="YYYY-MM-DD[THH:MM:SS]",
                   help="Absolute window end (default: now)")
    # Output
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Emit raw JSON (pipe-friendly)")
    p.add_argument("--csv",  default=None, metavar="FILE",
                   help="Write per-invocation CSV to file")
    return p.parse_args()


# ── Duration parser ───────────────────────────────────────────────────────────

def parse_duration(s: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)([smhd])", s.lower().strip())
    if not m:
        print(f"Error: cannot parse duration {s!r}. Use e.g. 30m, 4h, 7d.", file=sys.stderr)
        sys.exit(1)
    n, unit = int(m.group(1)), m.group(2)
    return {"s": dt.timedelta(seconds=n), "m": dt.timedelta(minutes=n),
            "h": dt.timedelta(hours=n),   "d": dt.timedelta(days=n)}[unit]


def _named_period(period: str) -> tuple:
    """Resolve a named period string to (start_dt, end_dt) in UTC."""
    now   = dt.datetime.now(dt.timezone.utc)
    today = now.date()

    if period == "today":
        start = dt.datetime(today.year, today.month, today.day, tzinfo=dt.timezone.utc)
        return start, now
    if period == "yesterday":
        d = today - dt.timedelta(days=1)
        start = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
        end   = dt.datetime(today.year, today.month, today.day, tzinfo=dt.timezone.utc)
        return start, end
    if period == "this-week":
        d = today - dt.timedelta(days=today.weekday())   # Monday
        start = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
        return start, now
    if period == "last-week":
        d     = today - dt.timedelta(days=today.weekday())  # this Monday
        end_d = d
        start_d = d - dt.timedelta(weeks=1)
        return (dt.datetime(start_d.year, start_d.month, start_d.day, tzinfo=dt.timezone.utc),
                dt.datetime(end_d.year,   end_d.month,   end_d.day,   tzinfo=dt.timezone.utc))
    if period == "this-month":
        start = dt.datetime(today.year, today.month, 1, tzinfo=dt.timezone.utc)
        return start, now
    if period == "last-month":
        first_this = dt.date(today.year, today.month, 1)
        last_month = first_this - dt.timedelta(days=1)
        start = dt.datetime(last_month.year, last_month.month, 1, tzinfo=dt.timezone.utc)
        end   = dt.datetime(first_this.year, first_this.month, first_this.day, tzinfo=dt.timezone.utc)
        return start, end
    raise ValueError(f"Unknown period: {period!r}")


def parse_window(args) -> tuple:
    """Return (start_dt, end_dt) as UTC-aware datetimes."""
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

    # --last or default 24h
    delta = parse_duration(args.last or "24h")
    return now - delta, now


# ── Client factory ────────────────────────────────────────────────────────────

def make_clients(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    connect = session.client("connect", region_name=resolved, config=RETRY_CONFIG)
    logs    = session.client("logs",    region_name=resolved, config=RETRY_CONFIG)
    return connect, logs


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ms(ts: dt.datetime) -> int:
    return int(ts.timestamp() * 1000)


def fetch_instance_alias(connect, instance_id):
    try:
        return connect.describe_instance(InstanceId=instance_id)["Instance"]["InstanceAlias"]
    except ClientError:
        return None


def filter_log_events(logs_client, log_group, filter_pattern, start_ms, end_ms):
    events = []
    kwargs = {
        "logGroupName":  log_group,
        "filterPattern": filter_pattern,
        "startTime":     start_ms,
        "endTime":       end_ms,
    }
    while True:
        try:
            resp = logs_client.filter_log_events(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                print(f"Error: log group {log_group!r} not found.", file=sys.stderr)
                sys.exit(1)
            print(f"Error querying logs [{code}]: {e.response['Error']['Message']}", file=sys.stderr)
            sys.exit(1)
        events.extend(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return events


def parse_message(raw: str) -> dict:
    try:
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return {}


# ── Log parsing ───────────────────────────────────────────────────────────────

def _event_ts(msg: dict, cw_ts_ms: int) -> dt.datetime:
    ts_str = msg.get("Timestamp")
    if ts_str:
        try:
            return dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(cw_ts_ms / 1000, tz=dt.timezone.utc)


def parse_invocations(cw_events: list, fn_match: str) -> list:
    """
    Extract every Lambda invocation for the target function from Connect flow logs.

    Returns list of dicts:
      {
        "contact_id":    str,
        "timestamp":     datetime,
        "function_arn":  str,
        "function_name": str,
        "flow_name":     str,
        "result":        "Success" | "Error" | "Unknown",
        "error_type":    str | None,   # None for successes
      }
    """
    fn_match_lower = fn_match.lower()
    results = []

    for ev in cw_events:
        msg = parse_message(ev["message"])
        if not msg:
            continue
        if msg.get("ContactFlowModuleType") not in _LAMBDA_MODULE_TYPES:
            continue

        params = msg.get("Parameters", {})
        arn    = params.get("FunctionArn") or params.get("LambdaFunctionARN") or ""
        if not arn:
            continue

        # Filter to the target function
        if fn_match_lower not in arn.lower():
            continue

        fn_name    = arn.split(":")[-1] if ":" in arn else arn
        contact_id = msg.get("ContactId") or msg.get("ContactFlowId") or ""
        ts         = _event_ts(msg, ev["timestamp"])
        flow_name  = msg.get("ContactFlowName") or ""

        external   = msg.get("ExternalResults") or msg.get("ExternalResult")
        error_type = msg.get("Error") or None

        if external:
            result     = "Success"
            error_type = None
        elif error_type:
            result = "Error"
        else:
            result     = "Unknown"
            error_type = "Unknown"

        results.append({
            "contact_id":   contact_id,
            "timestamp":    ts,
            "function_arn": arn,
            "function_name": fn_name,
            "flow_name":    flow_name,
            "result":       result,
            "error_type":   error_type,
        })

    return results


# ── Aggregation ───────────────────────────────────────────────────────────────

def aggregate(invocations: list) -> dict:
    """
    Returns:
      {
        "total":    int,
        "success":  int,
        "error":    int,
        "unknown":  int,
        "by_error": {error_type: [invocation, ...]},   # errors only, sorted by count desc
      }
    """
    by_error: dict = defaultdict(list)
    success = error = unknown = 0

    for inv in invocations:
        if inv["result"] == "Success":
            success += 1
        elif inv["result"] == "Error":
            error += 1
            by_error[inv["error_type"]].append(inv)
        else:
            unknown += 1
            by_error["Unknown"].append(inv)

    # Sort by count desc
    sorted_errors = dict(
        sorted(by_error.items(), key=lambda kv: len(kv[1]), reverse=True)
    )

    return {
        "total":    len(invocations),
        "success":  success,
        "error":    error + unknown,
        "unknown":  unknown,
        "by_error": sorted_errors,
    }


# ── Human-readable output ─────────────────────────────────────────────────────

def _hr():
    print("  " + "─" * 68)


def _pct(n, total):
    return f"{n / total * 100:.1f}%" if total else "—"


def print_human(fn_name, agg, start_dt, end_dt, log_group, fn_match):
    total   = agg["total"]
    success = agg["success"]
    errors  = agg["error"]

    _hr()
    print(f"  LAMBDA ERROR REPORT   {fn_name}")
    _hr()
    print(f"  Window   : {start_dt.strftime('%Y-%m-%d %H:%M')} UTC"
          f"  →  {end_dt.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Filter   : {fn_match!r}")
    print(f"  Log group: {log_group}\n")

    if total == 0:
        print("  No invocations found in this window.")
        _hr()
        print()
        return

    print(f"  Invocations : {total}")
    print(f"  Successes   : {success}  ({_pct(success, total)})")
    print(f"  Errors      : {errors}  ({_pct(errors, total)})")
    print(f"  Error types : {len(agg['by_error'])}")

    if not agg["by_error"]:
        print("\n  No errors found.")
        _hr()
        print()
        return

    print()
    _hr()
    print("  ERRORS BY TYPE")
    _hr()

    for error_type, invs in agg["by_error"].items():
        count = len(invs)
        print(f"\n  {error_type}  ({count} occurrence(s), {_pct(count, errors)} of errors)")
        shown = invs[:_MAX_DISPLAY]
        for inv in shown:
            ts_str   = inv["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            cid      = inv["contact_id"] or "(unknown)"
            flow_str = f"  [{inv['flow_name']}]" if inv["flow_name"] else ""
            print(f"    {ts_str} UTC   {cid}{flow_str}")
        if count > _MAX_DISPLAY:
            print(f"    \033[90m… {count - _MAX_DISPLAY} more — use --csv or --json for full list\033[0m")

    _hr()
    print()


# ── CSV output ────────────────────────────────────────────────────────────────

def write_csv(invocations: list, path: str):
    fields = ["timestamp", "contact_id", "function_name", "function_arn",
              "flow_name", "result", "error_type"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for inv in invocations:
            row = dict(inv)
            row["timestamp"] = inv["timestamp"].isoformat()
            w.writerow(row)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    connect, logs_client = make_clients(args.region, args.profile)
    start_dt, end_dt     = parse_window(args)

    # Resolve log group
    log_group = args.log_group
    if not log_group:
        alias = fetch_instance_alias(connect, args.instance_id)
        if alias:
            log_group = f"/aws/connect/{alias}"
        else:
            print(
                "Error: could not auto-discover Connect log group.\n"
                "Pass --log-group /aws/connect/<your-instance-alias> explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"  Log group : {log_group}", file=sys.stderr)
    print(f"  Window    : {start_dt.strftime('%Y-%m-%d %H:%M')} UTC"
          f"  →  {end_dt.strftime('%Y-%m-%d %H:%M')} UTC", file=sys.stderr)
    print(f"  Fetching flow logs matching {args.function!r}...", file=sys.stderr)

    cw_events   = filter_log_events(
        logs_client, log_group, f'"{args.function}"', _ms(start_dt), _ms(end_dt),
    )
    print(f"  {len(cw_events)} log event(s) found. Parsing...", file=sys.stderr)

    invocations = parse_invocations(cw_events, args.function)
    agg         = aggregate(invocations)

    # Derive display name from the first matching invocation's function_name
    fn_names = list({inv["function_name"] for inv in invocations if inv["function_name"]})
    fn_name  = fn_names[0] if len(fn_names) == 1 else args.function

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)

        doc = {
            "function":    fn_name,
            "fn_match":    args.function,
            "log_group":   log_group,
            "window": {
                "start": start_dt.isoformat(),
                "end":   end_dt.isoformat(),
            },
            "summary": {
                "total":    agg["total"],
                "success":  agg["success"],
                "errors":   agg["error"],
            },
            "errors": {
                etype: [
                    {
                        "contact_id":    inv["contact_id"],
                        "timestamp":     inv["timestamp"].isoformat(),
                        "function_name": inv["function_name"],
                        "flow_name":     inv["flow_name"],
                    }
                    for inv in invs
                ]
                for etype, invs in agg["by_error"].items()
            },
            "invocations": [
                {k: v.isoformat() if hasattr(v, "isoformat") else v
                 for k, v in inv.items()}
                for inv in invocations
            ],
        }
        print(json.dumps(doc, indent=2, default=serial))

    else:
        print_human(fn_name, agg, start_dt, end_dt, log_group, args.function)

    if args.csv:
        write_csv(invocations, args.csv)
        print(f"  Saved → {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
