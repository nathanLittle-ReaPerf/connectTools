#!/usr/bin/env python3
"""flow_traffic.py — Flow entry counts and per-contact flow paths from CloudWatch logs."""

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

import ct_config

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

_MAN = """\
NAME
    flow_traffic.py — Flow entry counts and per-contact flow paths

SYNOPSIS
    python flow_traffic.py --instance-id UUID [OPTIONS]

DESCRIPTION
    Reads the Connect CloudWatch flow-log group (FilterLogEvents) to show two
    complementary views:

      FLOW COUNTS — how many times each flow was entered, counting every entry
      separately (including re-entries when a transfer brings a contact back to
      a flow it already visited). Two numbers are shown per flow: total ENTRIES
      and unique CONTACTS.

      CONTACT PATHS — for each contact in the window, the ordered sequence of
      flows they traversed. Each flow appears once per entry in the sequence,
      not once per block executed inside it.

    Flow transitions are detected by tracking when ContactFlowId changes within
    a contact's events. If a contact loops back to a flow (e.g. Main IVR →
    Billing Flow → Main IVR), that second entry in Main IVR is counted
    separately — both in FLOW COUNTS and in the CONTACT PATHS sequence.

    Use --flow to narrow output to contacts that touched a specific flow.
    Use --contact-id to inspect a single contact.
    Use --no-paths to suppress the CONTACT PATHS section.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell
        region.

    --profile NAME
        AWS named profile for local development.

    --log-group NAME
        Override the auto-discovered Connect log group (/aws/connect/<alias>).

    --flow NAME
        Filter to contacts that touched this flow (case-insensitive substring).
        Applies to both FLOW COUNTS and CONTACT PATHS.

    --contact-id UUID
        Show paths and counts for a single contact only.

    --last DURATION
        Relative window ending now. Examples: 1h, 4h, 7d. Default: 24h.
        Mutually exclusive with --start.

    --start YYYY-MM-DD[THH:MM:SS]
        Absolute window start (UTC). Mutually exclusive with --last.

    --end YYYY-MM-DD[THH:MM:SS]
        Absolute window end (UTC). Default: now. Used with --start.

    --max N
        Stop paginating once N unique contacts are seen. Default: 200.
        Use 0 for no limit (may be slow on high-volume instances).

    --no-paths
        Print flow counts only; omit the per-contact paths table.

    --csv FILE
        Write contact paths to a CSV file. Columns: contact_id, start_time,
        flow_count, path. Saved under ~/.connecttools/FlowTraffic/ unless an
        absolute path is given.

    --json
        Print all results as JSON to stdout.

EXAMPLES
    # Last 24h (default)
    python flow_traffic.py --instance-id <UUID> --region us-east-1

    # Last 7 days
    python flow_traffic.py --instance-id <UUID> --last 7d

    # Contacts that touched "Billing IVR"
    python flow_traffic.py --instance-id <UUID> --flow "Billing IVR"

    # Single contact
    python flow_traffic.py --instance-id <UUID> --contact-id <UUID>

    # Counts only, no per-contact paths
    python flow_traffic.py --instance-id <UUID> --no-paths

    # JSON — pipe to jq
    python flow_traffic.py --instance-id <UUID> --json | jq '.counts[:5]'

    # Export contact paths to CSV
    python flow_traffic.py --instance-id <UUID> --csv paths.csv

IAM PERMISSIONS
    connect:DescribeInstance
    logs:FilterLogEvents on /aws/connect/<instance-alias>
"""


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Flow entry counts and per-contact flow paths from CloudWatch logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --last 7d
  %(prog)s --instance-id <UUID> --flow "Billing IVR"
  %(prog)s --instance-id <UUID> --contact-id <UUID>
  %(prog)s --instance-id <UUID> --no-paths
  %(prog)s --instance-id <UUID> --json | jq '.counts[:5]'
        """,
    )
    p.add_argument("--instance-id",  required=True, metavar="UUID")
    p.add_argument("--region",       default=None)
    p.add_argument("--profile",      default=None)
    p.add_argument("--log-group",    default=None, metavar="NAME")
    p.add_argument("--flow",         default=None, metavar="NAME",
                   help="Filter to contacts touching this flow (case-insensitive substring)")
    p.add_argument("--contact-id",   default=None, metavar="UUID")
    tg = p.add_mutually_exclusive_group()
    tg.add_argument("--last",  default=None, metavar="DURATION",
                    help="Relative window: 1h, 4h, 7d (default: 24h)")
    tg.add_argument("--start", default=None, metavar="YYYY-MM-DD[THH:MM:SS]")
    p.add_argument("--end",    default=None, metavar="YYYY-MM-DD[THH:MM:SS]")
    p.add_argument("--max",    type=int, default=200, metavar="N",
                   help="Max contacts to fetch (default 200; 0 = unlimited)")
    p.add_argument("--no-paths", action="store_true",
                   help="Show flow counts only; suppress the contact paths table")
    p.add_argument("--csv",    default=None, metavar="FILE")
    p.add_argument("--json",   action="store_true", dest="output_json")
    p.add_argument("--output", default=None, metavar="FILE",
                   help="Save JSON output to a file (implies --json)")
    return p.parse_args()


# ── Time window ───────────────────────────────────────────────────────────────

def _parse_duration(s: str) -> dt.timedelta:
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
    delta = _parse_duration(args.last) if args.last else dt.timedelta(hours=24)
    return now - delta, now


# ── AWS clients ───────────────────────────────────────────────────────────────

def make_clients(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    connect = session.client("connect", region_name=resolved, config=RETRY_CONFIG)
    logs    = session.client("logs",    region_name=resolved, config=RETRY_CONFIG)
    return connect, logs


# ── Log group resolution ──────────────────────────────────────────────────────

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


# ── Event fetching ────────────────────────────────────────────────────────────

def fetch_events(
    logs_client,
    log_group: str,
    start: dt.datetime,
    end: dt.datetime,
    max_contacts: int,
    contact_id: str | None = None,
) -> tuple:
    """Page through FilterLogEvents; stop once max_contacts unique contacts seen."""
    kwargs: dict = {
        "logGroupName": log_group,
        "startTime": int(start.timestamp() * 1000),
        "endTime":   int(end.timestamp() * 1000),
        "limit": 10000,
    }
    if contact_id:
        kwargs["filterPattern"] = f'{{ $.ContactId = "{contact_id}" }}'

    events: list = []
    seen: set    = set()
    page_num     = 0

    while True:
        page_num += 1
        try:
            resp = logs_client.filter_log_events(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                print(f"\n  Log group not found: {log_group}", file=sys.stderr)
                sys.exit(1)
            print(f"\n  Error fetching events: {e.response['Error']['Message']}", file=sys.stderr)
            sys.exit(1)

        for ev in resp.get("events", []):
            try:
                msg = json.loads(ev.get("message", ""))
            except Exception:
                msg = {}
            cid = msg.get("ContactId", "")
            if cid:
                if cid not in seen:
                    if max_contacts > 0 and len(seen) >= max_contacts:
                        continue
                    seen.add(cid)
            events.append({"timestamp": ev.get("timestamp"), "parsed": msg})

        print(
            f"\r  Page {page_num}: {len(events):,} events, {len(seen):,} contacts ...",
            end="", flush=True, file=sys.stderr,
        )

        token = resp.get("nextToken")
        if not token:
            break
        if max_contacts > 0 and len(seen) >= max_contacts:
            break
        kwargs["nextToken"] = token

    print(file=sys.stderr)

    # Trim events for contacts beyond the limit
    if max_contacts > 0:
        events = [ev for ev in events
                  if not ev["parsed"].get("ContactId")
                  or ev["parsed"].get("ContactId") in seen]

    return events, seen


# ── Sequence building ─────────────────────────────────────────────────────────

def build_sequences(events: list) -> dict:
    """
    Group events by ContactId, detect flow transitions (ContactFlowId changes),
    return per-contact data keyed by contact_id:
        {"start_ts": <epoch_ms>, "path": [{"id", "name", "ts"}, ...]}
    """
    by_contact: dict = defaultdict(list)
    for ev in events:
        cid = ev["parsed"].get("ContactId", "")
        if cid:
            by_contact[cid].append(ev)

    result = {}
    for cid, evs in by_contact.items():
        sorted_evs = sorted(evs, key=lambda e: e["timestamp"] or 0)
        start_ts   = sorted_evs[0]["timestamp"] if sorted_evs else 0

        path: list = []
        last_flow_id = ""
        for ev in sorted_evs:
            flow_id   = ev["parsed"].get("ContactFlowId", "")
            flow_name = ev["parsed"].get("ContactFlowName", "")
            if flow_id and flow_id != last_flow_id:
                path.append({"id": flow_id, "name": flow_name, "ts": ev["timestamp"]})
                last_flow_id = flow_id

        result[cid] = {"start_ts": start_ts, "path": path}

    return result


# ── Aggregation ───────────────────────────────────────────────────────────────

def compute_counts(sequences: dict) -> list:
    """
    Returns per-flow counts sorted by entries desc.
    entries  = total times any contact entered this flow (re-entries counted)
    contacts = unique contacts that entered this flow at least once
    """
    entries_by_name: dict  = defaultdict(int)
    contacts_by_name: dict = defaultdict(set)

    for cid, data in sequences.items():
        for step in data["path"]:
            name = step["name"] or step["id"]
            entries_by_name[name] += 1
            contacts_by_name[name].add(cid)

    rows = [
        {"flow": name, "entries": entries_by_name[name], "contacts": len(contacts_by_name[name])}
        for name in entries_by_name
    ]
    rows.sort(key=lambda r: r["entries"], reverse=True)
    return rows


def filter_by_flow(sequences: dict, flow_filter: str) -> dict:
    """Keep only contacts whose path contains a flow matching flow_filter."""
    needle = flow_filter.lower()
    return {
        cid: data
        for cid, data in sequences.items()
        if any(needle in (step["name"] or "").lower() for step in data["path"])
    }


# ── Formatting helpers ────────────────────────────────────────────────────────

def _hr():
    print("  " + "─" * 76)


def _ts(epoch_ms: int | None) -> str:
    if not epoch_ms:
        return "?"
    return dt.datetime.fromtimestamp(epoch_ms / 1000, tz=dt.timezone.utc).strftime("%m-%d %H:%M")


def _path_str(path: list) -> str:
    names = [step["name"] or step["id"][:8] for step in path]
    return " > ".join(names) if names else "(no flows)"


# ── Human output ──────────────────────────────────────────────────────────────

def print_human(counts, sequences, start, end, instance_id, no_paths):
    _hr()
    print(f"  FLOW TRAFFIC   {instance_id}")
    _hr()
    fmt = "%Y-%m-%d %H:%M"
    print(f"  {start.strftime(fmt)} → {end.strftime(fmt)} UTC  ·  {len(sequences):,} contacts\n")

    if not counts:
        print("  No flow log data found for the requested window.")
        print()
        _hr()
        print()
        return

    # ── FLOW COUNTS ───────────────────────────────────────────────────────────
    flow_w = max(max((len(r["flow"]) for r in counts), default=4), 4)
    print(f"  FLOW COUNTS")
    print(f"  {'FLOW':<{flow_w}}  {'ENTRIES':>8}  {'CONTACTS':>8}")
    print(f"  {'─' * flow_w}  {'─' * 8}  {'─' * 8}")
    for r in counts:
        print(f"  {r['flow']:<{flow_w}}  {r['entries']:>8,}  {r['contacts']:>8,}")
    total_entries = sum(r["entries"] for r in counts)
    print(f"\n  {len(counts)} flow(s)  ·  {total_entries:,} total entries")

    if no_paths:
        print()
        _hr()
        print()
        return

    # ── CONTACT PATHS ─────────────────────────────────────────────────────────
    print()
    sorted_contacts = sorted(sequences.items(), key=lambda kv: kv[1]["start_ts"] or 0, reverse=True)
    print(f"  CONTACT PATHS  ({len(sorted_contacts):,} contacts, newest first)\n")
    for cid, data in sorted_contacts:
        path_str = _path_str(data["path"])
        print(f"  Contact ID: {cid}   {path_str}")

    print()
    _hr()
    print()


# ── CSV / JSON output ─────────────────────────────────────────────────────────

def write_csv(sequences: dict, path: str):
    out = ct_config.output_dir("flow_traffic") / path if not path.startswith(("/", "\\")) and ":" not in path else None
    dest = str(out) if out else path
    sorted_contacts = sorted(sequences.items(), key=lambda kv: kv[1]["start_ts"] or 0, reverse=True)
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["contact_id", "start_time", "flow_count", "path"])
        for cid, data in sorted_contacts:
            ts_str   = _ts(data["start_ts"])
            path_str = " -> ".join(step["name"] or step["id"] for step in data["path"])
            w.writerow([cid, ts_str, len(data["path"]), path_str])
    return dest


def _serial(obj):
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    raise TypeError(f"Not serializable: {type(obj)}")


def build_json(counts, sequences, start, end, instance_id) -> dict:
    contacts = []
    for cid, data in sorted(sequences.items(), key=lambda kv: kv[1]["start_ts"] or 0, reverse=True):
        contacts.append({
            "contact_id": cid,
            "start_time": _ts(data["start_ts"]),
            "flow_count":  len(data["path"]),
            "path": [
                {"name": s["name"], "id": s["id"], "ts": _ts(s["ts"])}
                for s in data["path"]
            ],
        })
    return {
        "instance_id": instance_id,
        "window": {
            "start": start.isoformat(),
            "end":   end.isoformat(),
        },
        "counts":   counts,
        "contacts": contacts,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args           = parse_args()
    connect, logs  = make_clients(args.region, args.profile)
    start, end     = parse_window(args)
    log_group      = resolve_log_group(connect, args.instance_id, args.log_group)

    fmt = "%Y-%m-%d %H:%M UTC"
    print(f"  Log group : {log_group}", file=sys.stderr)
    print(f"  Window    : {start.strftime(fmt)} → {end.strftime(fmt)}", file=sys.stderr)
    if args.contact_id:
        print(f"  Contact   : {args.contact_id}", file=sys.stderr)
    elif args.flow:
        print(f"  Flow filter: {args.flow!r}", file=sys.stderr)
    max_label = str(args.max) if args.max > 0 else "unlimited"
    if not args.contact_id:
        print(f"  Max       : {max_label} contacts", file=sys.stderr)
    print(file=sys.stderr)

    events, _ = fetch_events(
        logs, log_group, start, end,
        max_contacts=0 if args.contact_id else args.max,
        contact_id=args.contact_id or None,
    )

    if not events:
        print("  No events found in the requested window.", file=sys.stderr)
        sys.exit(0)

    sequences = build_sequences(events)

    if args.flow:
        sequences = filter_by_flow(sequences, args.flow)
        if not sequences:
            print(f"  No contacts found touching flow {args.flow!r}.", file=sys.stderr)
            sys.exit(0)

    counts = compute_counts(sequences)

    if args.output_json or args.output:
        payload = json.dumps(build_json(counts, sequences, start, end, args.instance_id), indent=2)
        if args.output:
            out = ct_config.output_dir("flow_traffic") / args.output if not args.output.startswith(("/", "\\")) and ":" not in args.output else args.output
            with open(str(out), "w", encoding="utf-8") as f:
                f.write(payload)
            print(f"  Saved → {out}", file=sys.stderr)
        else:
            print(payload)
        return

    print_human(counts, sequences, start, end, args.instance_id, args.no_paths)

    if args.csv:
        dest = write_csv(sequences, args.csv)
        print(f"  Saved → {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
