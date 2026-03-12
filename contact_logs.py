#!/usr/bin/env python3
"""contact_logs.py — Download CloudWatch flow-execution logs for a Connect contact ID."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})


# ── Argument parsing ───────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Download CloudWatch flow-execution logs for an Amazon Connect contact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --contact-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --contact-id <UUID> --text
  %(prog)s --instance-id <UUID> --contact-id <UUID> --output my_logs.json
  %(prog)s --instance-id <UUID> --contact-id <UUID> --log-group /aws/connect/my-instance
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--contact-id",  required=True, metavar="UUID")
    p.add_argument("--region",      default=None,  help="AWS region (defaults to session/CloudShell region)")
    p.add_argument("--profile",     default=None,  help="AWS named profile")
    p.add_argument("--log-group",   default=None,  metavar="NAME",
                   help="Override auto-discovered log group (default: /aws/connect/<instance-alias>)")
    p.add_argument("--text",        action="store_true",
                   help="Plain-text output instead of JSON")
    p.add_argument("--output",      default=None,  metavar="FILE",
                   help="Output file path (default: <contact-id>_logs.json or .txt)")
    return p.parse_args()


# ── Client factory ─────────────────────────────────────────────────────────────

def make_clients(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    connect = session.client("connect", region_name=resolved, config=RETRY_CONFIG)
    logs    = session.client("logs",    region_name=resolved, config=RETRY_CONFIG)
    return connect, logs


# ── Connect fetchers ───────────────────────────────────────────────────────────

def fetch_contact(connect, instance_id, contact_id):
    try:
        return connect.describe_contact(InstanceId=instance_id, ContactId=contact_id)["Contact"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        print(f"Error fetching contact [{code}]: {msg}", file=sys.stderr)
        sys.exit(1)


def fetch_instance_alias(connect, instance_id):
    try:
        return connect.describe_instance(InstanceId=instance_id)["Instance"]["InstanceAlias"]
    except ClientError:
        return None


# ── CloudWatch Logs fetcher ────────────────────────────────────────────────────

def fetch_log_events(logs_client, log_group, contact_id, start_ms, end_ms):
    """Paginate FilterLogEvents; return list of raw event dicts."""
    events, kwargs = [], {
        "logGroupName":  log_group,
        "filterPattern": f'"{contact_id}"',
        "startTime":     start_ms,
        "endTime":       end_ms,
    }
    while True:
        try:
            resp = logs_client.filter_log_events(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            msg  = e.response["Error"]["Message"]
            print(f"Error querying logs [{code}]: {msg}", file=sys.stderr)
            sys.exit(1)
        events.extend(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return events


# ── Output helpers ─────────────────────────────────────────────────────────────

def _ms(ts: dt.datetime) -> int:
    return int(ts.timestamp() * 1000)


def _parse_message(raw: str) -> dict:
    """Try to parse the message as JSON; fall back to raw string."""
    try:
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return {"raw": raw.strip()}


def write_json(events, contact_id, log_group, start_ts, end_ts, out_path):
    def serial(o):
        return o.isoformat() if hasattr(o, "isoformat") else str(o)

    doc = {
        "contact_id":  contact_id,
        "log_group":   log_group,
        "window": {
            "start": start_ts.isoformat(),
            "end":   end_ts.isoformat() if end_ts else None,
        },
        "event_count": len(events),
        "events": [
            {
                "timestamp":  dt.datetime.fromtimestamp(
                    ev["timestamp"] / 1000, tz=dt.timezone.utc
                ).isoformat(),
                "log_stream": ev.get("logStreamName"),
                "message":    _parse_message(ev["message"]),
            }
            for ev in events
        ],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, default=serial)


def write_text(events, out_path):
    lines = []
    for ev in events:
        ts  = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, tz=dt.timezone.utc)
        msg = ev["message"].rstrip()
        lines.append(f"{ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} UTC  {msg}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    connect, logs_client = make_clients(args.region, args.profile)

    # Fetch contact for precise time bounds
    contact  = fetch_contact(connect, args.instance_id, args.contact_id)
    start_ts = contact.get("InitiationTimestamp")
    end_ts   = contact.get("DisconnectTimestamp")

    if start_ts is None:
        print("Error: contact has no InitiationTimestamp.", file=sys.stderr)
        sys.exit(1)

    # Buffer: 2 min before initiation, 5 min after disconnect (or now if still active)
    now      = dt.datetime.now(dt.timezone.utc)
    start_ms = _ms(start_ts - dt.timedelta(minutes=2))
    end_ms   = _ms(min(end_ts + dt.timedelta(minutes=5), now) if end_ts else now)

    # Resolve log group
    log_group = args.log_group
    if not log_group:
        alias = fetch_instance_alias(connect, args.instance_id)
        if alias:
            log_group = f"/aws/connect/{alias}"
        else:
            print(
                "Error: could not auto-discover log group from instance alias.\n"
                "Pass --log-group /aws/connect/<your-instance-alias> explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"  Log group : {log_group}", file=sys.stderr)
    print(
        f"  Window    : {start_ts.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        f" → {(end_ts + dt.timedelta(minutes=5) if end_ts else now).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        file=sys.stderr,
    )

    events = fetch_log_events(logs_client, log_group, args.contact_id, start_ms, end_ms)

    if not events:
        print(f"  No log events found for {args.contact_id}.", file=sys.stderr)
        sys.exit(0)

    print(f"  Found {len(events)} event(s).", file=sys.stderr)

    ext      = "txt" if args.text else "json"
    out_path = args.output or f"{args.contact_id}_logs.{ext}"

    if args.text:
        write_text(events, out_path)
    else:
        write_json(events, args.contact_id, log_group, start_ts, end_ts, out_path)

    print(f"  Saved → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
