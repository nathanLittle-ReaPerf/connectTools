#!/usr/bin/env python3
"""lambda_tracer.py — Trace Lambda invocations for an Amazon Connect contact.

Pulls Connect flow-execution logs for the contact, finds every Lambda invocation,
then fetches the actual Lambda CloudWatch logs for each function around the
invocation timestamp.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

# Seconds either side of the Connect-reported invocation time to search Lambda logs
LAMBDA_WINDOW_SECS = 30

_MAN = """\
NAME
    lambda_tracer.py — Trace Lambda invocations for an Amazon Connect contact

SYNOPSIS
    python lambda_tracer.py --instance-id UUID --contact-id UUID [OPTIONS]

DESCRIPTION
    Pulls Connect flow-execution logs for a contact, finds every Lambda invocation
    (InvokeExternalResource / InvokeLambdaFunction blocks), and fetches the actual
    Lambda CloudWatch log lines within a ±30-second window around each invocation
    timestamp. Useful for diagnosing Lambda failures that affected a specific call.
    Use --summary for a fast overview without fetching Lambda logs; you can then
    enter an invocation number to drill down interactively.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --contact-id UUID
        Contact UUID. Required.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --log-group NAME
        Override the auto-discovered Connect CloudWatch log group.
        Default: /aws/connect/<instance-alias>.

    --summary
        Show invocation metadata only (ARN, timestamp, result, response) without
        fetching Lambda log lines. After output, prompts to drill down by number.

    --json
        Print the full trace as JSON to stdout.

    --output FILE
        Write JSON output to a file (default: <contact-id>_lambda_trace.json).

EXAMPLES
    # Full trace with Lambda log lines
    python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

    # Summary only (no Lambda log fetch)
    python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --summary

    # JSON output saved to file
    python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --output trace.json

    # Override log group
    python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> \\
        --log-group /aws/connect/myInstance

IAM PERMISSIONS
    connect:DescribeContact
    connect:DescribeInstance
    logs:FilterLogEvents (on /aws/connect/<instance-alias>)
    logs:FilterLogEvents (on /aws/lambda/<function-name> for each invoked function)

NOTES
    Lambda logs are fetched within ±30 seconds of the Connect-reported invocation
    timestamp. High-concurrency Lambda functions may include unrelated log lines
    from concurrent invocations in that window. The log group is case-sensitive
    and auto-discovered from the instance alias; pass --log-group to override.
"""


# ── Argument parsing ───────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Trace Lambda invocations for an Amazon Connect contact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --contact-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --contact-id <UUID> --json
  %(prog)s --instance-id <UUID> --contact-id <UUID> --log-group /aws/connect/myInstance
        """,
    )
    p.add_argument("--instance-id", required=True,  metavar="UUID")
    p.add_argument("--contact-id",  required=True,  metavar="UUID")
    p.add_argument("--region",      default=None,   help="AWS region (defaults to session/CloudShell region)")
    p.add_argument("--profile",     default=None,   help="AWS named profile")
    p.add_argument("--log-group",   default=None,   metavar="NAME",
                   help="Override auto-discovered Connect log group")
    p.add_argument("--output",      default=None,   metavar="FILE",
                   help="Write JSON output to file (default: <contact-id>_lambda_trace.json)")
    p.add_argument("--json",        action="store_true", dest="output_json",
                   help="Print JSON to stdout instead of human-readable output")
    p.add_argument("--summary",     action="store_true",
                   help="Show invocation summary only — skip fetching Lambda log lines")
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


# ── Connect helpers ────────────────────────────────────────────────────────────

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


# ── CloudWatch Logs helpers ────────────────────────────────────────────────────

def _ms(ts: dt.datetime) -> int:
    return int(ts.timestamp() * 1000)


def filter_log_events(logs_client, log_group, filter_pattern, start_ms, end_ms):
    """Paginate FilterLogEvents; return list of raw event dicts. Returns [] on missing log group."""
    events, kwargs = [], {
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
                return []
            print(f"Error querying {log_group} [{code}]: {e.response['Error']['Message']}",
                  file=sys.stderr)
            return []
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
        return {"raw": raw.strip()}


# ── Connect log parsing ────────────────────────────────────────────────────────

# Connect logs Lambda invocations as ContactFlowModuleType = InvokeExternalResource
_LAMBDA_MODULE_TYPES = {"InvokeExternalResource", "InvokeLambdaFunction"}


def extract_lambda_invocations(events: list) -> list:
    """
    Parse Connect flow-execution log events and return one dict per Lambda invocation:
      {
        "function_arn":  str,
        "function_name": str,
        "invoked_at":    datetime (UTC),
        "result":        "Success" | "Error" | unknown,
        "connect_response": dict | None,   # ExternalResults from Connect's perspective
        "flow_name":     str | None,
        "raw_entry":     dict,
      }
    """
    invocations = []
    for ev in events:
        msg = parse_message(ev["message"])
        if not isinstance(msg, dict):
            continue
        if msg.get("ContactFlowModuleType") not in _LAMBDA_MODULE_TYPES:
            continue

        params  = msg.get("Parameters", {})
        arn     = params.get("FunctionArn") or params.get("LambdaFunctionARN") or ""
        if not arn:
            continue

        # Derive function name from ARN (last segment)
        function_name = arn.split(":")[-1] if ":" in arn else arn

        # Timestamp — prefer the message's own Timestamp field, fall back to CW event time
        ts_str = msg.get("Timestamp")
        if ts_str:
            try:
                invoked_at = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                invoked_at = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, tz=dt.timezone.utc)
        else:
            invoked_at = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, tz=dt.timezone.utc)

        # Connect-side result
        external = msg.get("ExternalResults") or msg.get("ExternalResult")
        result   = "Success" if external else msg.get("Error", "Unknown")

        invocations.append({
            "function_arn":      arn,
            "function_name":     function_name,
            "invoked_at":        invoked_at,
            "result":            result,
            "connect_response":  external,
            "flow_name":         msg.get("ContactFlowName"),
            "raw_entry":         msg,
        })

    return invocations


# ── Lambda log fetching ────────────────────────────────────────────────────────

def fetch_lambda_logs(logs_client, function_name: str, invoked_at: dt.datetime) -> list:
    """
    Search /aws/lambda/<function_name> for log events within LAMBDA_WINDOW_SECS
    of the invocation timestamp. Returns list of {timestamp, message} dicts.
    """
    log_group = f"/aws/lambda/{function_name}"
    window    = dt.timedelta(seconds=LAMBDA_WINDOW_SECS)
    start_ms  = _ms(invoked_at - window)
    end_ms    = _ms(invoked_at + window)

    raw = filter_log_events(logs_client, log_group, "", start_ms, end_ms)
    return [
        {
            "timestamp": dt.datetime.fromtimestamp(
                e["timestamp"] / 1000, tz=dt.timezone.utc
            ).isoformat(),
            "message": e["message"].rstrip(),
        }
        for e in raw
    ]


# ── Output ─────────────────────────────────────────────────────────────────────

def _hr():
    print("  " + "─" * 64)


def print_human(contact_id, invocations_with_logs, show_logs=True):
    _hr()
    print(f"  LAMBDA TRACE   {contact_id}")
    _hr()

    if not invocations_with_logs:
        print("\n  No Lambda invocations found in Connect flow logs.\n")
        return

    print(f"\n  {len(invocations_with_logs)} invocation(s) found.\n")

    for i, item in enumerate(invocations_with_logs, 1):
        inv  = item["invocation"]
        logs = item["lambda_logs"]

        print(f"  [{i}] {inv['function_name']}")
        print(f"       ARN       : {inv['function_arn']}")
        print(f"       Invoked   : {inv['invoked_at'].strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} UTC")
        print(f"       Flow      : {inv['flow_name'] or '(unknown)'}")
        print(f"       Result    : {inv['result']}")
        if inv["connect_response"]:
            resp_str = json.dumps(inv["connect_response"], separators=(",", ":"))
            if len(resp_str) > 120:
                resp_str = resp_str[:117] + "..."
            print(f"       Response  : {resp_str}")

        if show_logs:
            print(f"\n       Lambda logs (±{LAMBDA_WINDOW_SECS}s window):")
            if logs:
                for entry in logs:
                    ts  = entry["timestamp"][11:23]   # HH:MM:SS.mmm
                    msg = entry["message"]
                    if len(msg) > 200:
                        msg = msg[:197] + "..."
                    print(f"         {ts}  {msg}")
            else:
                print(f"         (no log events found — check IAM or log group /aws/lambda/{inv['function_name']})")
        print()

    _hr()
    print()


# ── Interactive drill-down ─────────────────────────────────────────────────────

def drill_down_loop(logs_client, invocations_with_logs):
    """After showing the summary, let the user pick an invocation to see full logs."""
    if not invocations_with_logs:
        return
    count = len(invocations_with_logs)
    while True:
        try:
            raw = input(f"  Enter invocation number for full logs (1-{count}), or Enter to exit: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            break
        try:
            idx = int(raw)
        except ValueError:
            print(f"  Please enter a number between 1 and {count}.")
            continue
        if idx < 1 or idx > count:
            print(f"  Please enter a number between 1 and {count}.")
            continue

        item = invocations_with_logs[idx - 1]
        inv  = item["invocation"]

        # Fetch on demand if not already retrieved
        if not item["lambda_logs"]:
            print(f"  Fetching logs for {inv['function_name']}...", file=sys.stderr)
            item["lambda_logs"] = fetch_lambda_logs(logs_client, inv["function_name"], inv["invoked_at"])

        logs = item["lambda_logs"]
        _hr()
        print(f"  [{idx}] {inv['function_name']}  —  Lambda logs (±{LAMBDA_WINDOW_SECS}s window)")
        _hr()
        if logs:
            for entry in logs:
                ts  = entry["timestamp"][11:23]
                msg = entry["message"].rstrip()
                if len(msg) > 200:
                    msg = msg[:197] + "..."
                print(f"    {ts}  {msg}")
        else:
            print(f"    (no log events found — check IAM or log group /aws/lambda/{inv['function_name']})")
        _hr()
        print()

        # Offer to save to file
        try:
            dest = input("  Save to file? Enter filename (or Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if dest:
            def serial(o):
                return o.isoformat() if hasattr(o, "isoformat") else str(o)
            doc = {
                "function_name":    inv["function_name"],
                "function_arn":     inv["function_arn"],
                "invoked_at":       inv["invoked_at"].isoformat(),
                "result":           inv["result"],
                "connect_response": inv["connect_response"],
                "flow_name":        inv["flow_name"],
                "lambda_logs":      logs,
            }
            try:
                with open(dest, "w", encoding="utf-8") as f:
                    json.dump(doc, f, indent=2, default=serial)
                print(f"  Saved → {dest}")
            except OSError as e:
                print(f"  Error saving: {e}")
        print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    args = parse_args()
    connect, logs_client = make_clients(args.region, args.profile)

    contact    = fetch_contact(connect, args.instance_id, args.contact_id)
    start_ts   = contact.get("InitiationTimestamp")
    end_ts     = contact.get("DisconnectTimestamp")

    if start_ts is None:
        print("Error: contact has no InitiationTimestamp.", file=sys.stderr)
        sys.exit(1)

    now      = dt.datetime.now(dt.timezone.utc)
    start_ms = _ms(start_ts - dt.timedelta(minutes=2))
    end_ms   = _ms(min(end_ts + dt.timedelta(minutes=5), now) if end_ts else now)

    # Resolve Connect log group
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

    print(f"  Connect log group : {log_group}", file=sys.stderr)
    print(f"  Fetching flow logs...", file=sys.stderr)

    connect_events = filter_log_events(
        logs_client, log_group,
        f'{{ $.ContactId = "{args.contact_id}" }}',
        start_ms, end_ms,
    )

    if not connect_events:
        print(f"  No Connect flow log events found for {args.contact_id}.", file=sys.stderr)
        sys.exit(0)

    invocations = extract_lambda_invocations(connect_events)

    if args.summary:
        print(f"  Found {len(invocations)} Lambda invocation(s). Summary only.", file=sys.stderr)
        invocations_with_logs = [{"invocation": inv, "lambda_logs": []} for inv in invocations]
    else:
        print(f"  Found {len(invocations)} Lambda invocation(s). Fetching Lambda logs...", file=sys.stderr)
        seen_functions: set = set()
        invocations_with_logs = []
        for inv in invocations:
            fname = inv["function_name"]
            if fname not in seen_functions:
                seen_functions.add(fname)
                print(f"    /aws/lambda/{fname}", file=sys.stderr)
            lambda_logs = fetch_lambda_logs(logs_client, fname, inv["invoked_at"])
            invocations_with_logs.append({"invocation": inv, "lambda_logs": lambda_logs})

    # Output
    if args.output_json or args.output:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)

        doc = {
            "contact_id":   args.contact_id,
            "connect_log_group": log_group,
            "invocation_count": len(invocations),
            "invocations": [
                {
                    "function_arn":      item["invocation"]["function_arn"],
                    "function_name":     item["invocation"]["function_name"],
                    "invoked_at":        item["invocation"]["invoked_at"].isoformat(),
                    "result":            item["invocation"]["result"],
                    "connect_response":  item["invocation"]["connect_response"],
                    "flow_name":         item["invocation"]["flow_name"],
                    "lambda_log_count":  len(item["lambda_logs"]),
                    "lambda_logs":       item["lambda_logs"],
                }
                for item in invocations_with_logs
            ],
        }
        out = json.dumps(doc, indent=2, default=serial)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"  Saved → {args.output}", file=sys.stderr)
        else:
            print(out)
    else:
        print_human(args.contact_id, invocations_with_logs, show_logs=not args.summary)
        if args.summary:
            drill_down_loop(logs_client, invocations_with_logs)


if __name__ == "__main__":
    main()
