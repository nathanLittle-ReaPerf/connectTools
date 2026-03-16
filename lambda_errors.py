#!/usr/bin/env python3
"""lambda_errors.py — Aggregate Lambda errors from CloudWatch Logs.

Searches /aws/lambda/<function-name> for error events over a time window,
classifies them by error type, and groups occurrences — useful for spotting
patterns in Lambda failures that affect Amazon Connect contacts.
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

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})
_MAX_DISPLAY = 15   # max occurrences shown per error type in human output

# CloudWatch filter to catch error-level Lambda log lines
_LAMBDA_ERROR_FILTER = '?ERROR ?"Task timed out" ?"errorType" ?"Traceback" ?"Exception"'

# Regexes for parsing
_UUID_RE      = re.compile(r'\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b')
_EXC_RE       = re.compile(r'([A-Za-z][A-Za-z0-9_]*(?:Exception|Error|Fault))[:\s]')

_MAN = """\
NAME
    lambda_errors.py — Aggregate Lambda errors from CloudWatch Logs

SYNOPSIS
    python lambda_errors.py --function NAME [OPTIONS]

DESCRIPTION
    Searches the Lambda function's CloudWatch log group (/aws/lambda/<name>)
    for error events over a time window, classifies them by error type, and
    groups occurrences. Useful for spotting error patterns in Lambda functions
    invoked by Amazon Connect flows.

    The function argument can be a function name, a name fragment, or a full ARN.
    The function name is extracted from an ARN automatically.

OPTIONS
    --function NAME
        Lambda function name, name fragment, or full ARN. Required.
        Used to derive the log group: /aws/lambda/<function-name>.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --log-group NAME
        Override the auto-derived Lambda log group.

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
        Emit raw JSON with summary and full error list.

    --csv FILE
        Write per-error CSV to a file.

EXAMPLES
    # Errors for today (default window)
    python lambda_errors.py --function my-connect-lambda --region us-east-1

    # Yesterday's errors using a full ARN
    python lambda_errors.py \\
        --function arn:aws:lambda:us-east-1:123456789012:function:my-fn \\
        --period yesterday

    # Last 4 hours
    python lambda_errors.py --function my-connect-lambda --last 4h

    # Custom date range, export CSV
    python lambda_errors.py --function my-connect-lambda \\
        --start 2026-03-15 --end 2026-03-16 --csv errors.csv

    # JSON output
    python lambda_errors.py --function my-connect-lambda --json | jq '.by_type'

IAM PERMISSIONS
    logs:FilterLogEvents (on /aws/lambda/<function-name>)

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
  %(prog)s --function my-connect-lambda --region us-east-1
  %(prog)s --function my-connect-lambda --period yesterday
  %(prog)s --function arn:aws:lambda:us-east-1:123:function:my-fn --period last-week
  %(prog)s --function my-connect-lambda --last 4h
  %(prog)s --function my-connect-lambda --start 2026-03-15 --end 2026-03-16
        """,
    )
    p.add_argument("--function",  required=True, metavar="NAME",
                   help="Lambda function name, fragment, or full ARN")
    p.add_argument("--region",    default=None,  help="AWS region")
    p.add_argument("--profile",   default=None,  help="AWS named profile")
    p.add_argument("--log-group", default=None,  metavar="NAME",
                   help="Override auto-derived Lambda log group")
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
        d       = today - dt.timedelta(days=today.weekday())  # this Monday
        start_d = d - dt.timedelta(weeks=1)
        return (dt.datetime(start_d.year, start_d.month, start_d.day, tzinfo=dt.timezone.utc),
                dt.datetime(d.year,       d.month,       d.day,       tzinfo=dt.timezone.utc))
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

    delta = parse_duration(args.last or "24h")
    return now - delta, now


# ── Client factory ─────────────────────────────────────────────────────────────

def make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("logs", region_name=resolved, config=RETRY_CONFIG)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ms(ts: dt.datetime) -> int:
    return int(ts.timestamp() * 1000)


def extract_function_name(fn_arg: str) -> str:
    """Return bare function name from a full ARN or return the arg unchanged."""
    if fn_arg.startswith("arn:aws"):
        # arn:aws:lambda:region:account:function:name[:qualifier]
        parts = fn_arg.split(":")
        if len(parts) >= 7:
            return parts[6]
    return fn_arg


def filter_log_events(logs_client, log_group, filter_pattern, start_ms, end_ms):
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
    """Return (error_type, message) for a Lambda log line."""
    stripped = line.strip()

    # Lambda timeout
    if "Task timed out" in line:
        return "Timeout", stripped[:200]

    # Lambda JSON error response (unhandled exception)
    if stripped.startswith("{") and "errorType" in stripped:
        try:
            obj = json.loads(stripped)
            return obj.get("errorType", "Error"), obj.get("errorMessage", stripped[:200])
        except (json.JSONDecodeError, ValueError):
            pass

    # Exception class names in the log line
    m = _EXC_RE.search(line)
    if m:
        return m.group(1), stripped[:200]

    # Generic fallback
    return "Error", stripped[:200]


def parse_lambda_errors(cw_events: list) -> list:
    """
    Parse raw CloudWatch log events from a Lambda log group into error records.

    Returns list of dicts:
      { timestamp, request_id, error_type, message }
    """
    results = []
    for ev in cw_events:
        line    = ev["message"]
        ts      = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, tz=dt.timezone.utc)
        stripped = line.strip()

        # Skip Lambda platform lines — they are not errors
        if stripped.startswith(("START RequestId:", "END RequestId:", "REPORT RequestId:")):
            continue

        error_type, message = _classify_error(line)
        results.append({
            "timestamp":  ts,
            "request_id": _extract_request_id(line),
            "error_type": error_type,
            "message":    message,
        })

    return results


# ── Aggregation ────────────────────────────────────────────────────────────────

def aggregate(errors: list) -> dict:
    by_type: dict = defaultdict(list)
    for err in errors:
        by_type[err["error_type"]].append(err)

    sorted_by_type = dict(
        sorted(by_type.items(), key=lambda kv: len(kv[1]), reverse=True)
    )
    return {"total": len(errors), "by_type": sorted_by_type}


# ── Human-readable output ──────────────────────────────────────────────────────

def _hr():
    print("  " + "─" * 68)


def print_human(fn_name, agg, start_dt, end_dt, log_group):
    total = agg["total"]

    _hr()
    print(f"  LAMBDA ERROR REPORT   {fn_name}")
    _hr()
    print(f"  Window   : {start_dt.strftime('%Y-%m-%d %H:%M')} UTC"
          f"  →  {end_dt.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Log group: {log_group}\n")

    if total == 0:
        print("  No errors found in this window.")
        _hr()
        print()
        return

    print(f"  Error events : {total}")
    print(f"  Error types  : {len(agg['by_type'])}")

    print()
    _hr()
    print("  ERRORS BY TYPE")
    _hr()

    for error_type, errs in agg["by_type"].items():
        count = len(errs)
        print(f"\n  {error_type}  ({count} occurrence(s))")
        shown = errs[:_MAX_DISPLAY]
        for err in shown:
            ts_str = err["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            rid    = f"  [{err['request_id'][:8]}…]" if err.get("request_id") else ""
            msg    = err.get("message", "")
            # Trim long messages for display
            if len(msg) > 100:
                msg = msg[:99] + "…"
            print(f"    {ts_str} UTC{rid}")
            if msg and msg != err.get("error_type"):
                print(f"      \033[90m{msg}\033[0m")
        if count > _MAX_DISPLAY:
            print(f"    \033[90m… {count - _MAX_DISPLAY} more — use --csv or --json for full list\033[0m")

    _hr()
    print()


# ── CSV output ─────────────────────────────────────────────────────────────────

def write_csv(errors: list, path: str):
    fields = ["timestamp", "request_id", "error_type", "message"]
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
    logs_client          = make_client(args.region, args.profile)
    start_dt, end_dt     = parse_window(args)

    # Derive log group from function name
    fn_name   = extract_function_name(args.function)
    log_group = args.log_group or f"/aws/lambda/{fn_name}"

    print(f"  Log group : {log_group}", file=sys.stderr)
    print(f"  Window    : {start_dt.strftime('%Y-%m-%d %H:%M')} UTC"
          f"  →  {end_dt.strftime('%Y-%m-%d %H:%M')} UTC", file=sys.stderr)
    print(f"  Fetching Lambda error logs...", file=sys.stderr)

    cw_events = filter_log_events(
        logs_client, log_group, _LAMBDA_ERROR_FILTER, _ms(start_dt), _ms(end_dt),
    )
    print(f"  {len(cw_events)} matching log event(s). Parsing...", file=sys.stderr)

    errors = parse_lambda_errors(cw_events)
    agg    = aggregate(errors)

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)

        doc = {
            "function":  fn_name,
            "log_group": log_group,
            "window": {
                "start": start_dt.isoformat(),
                "end":   end_dt.isoformat(),
            },
            "summary": {
                "total":       agg["total"],
                "error_types": len(agg["by_type"]),
            },
            "by_type": {
                etype: [
                    {
                        "timestamp":  err["timestamp"].isoformat(),
                        "request_id": err.get("request_id"),
                        "message":    err.get("message"),
                    }
                    for err in errs
                ]
                for etype, errs in agg["by_type"].items()
            },
        }
        print(json.dumps(doc, indent=2, default=serial))
    else:
        print_human(fn_name, agg, start_dt, end_dt, log_group)

    if args.csv:
        write_csv(errors, args.csv)
        print(f"  Saved → {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
