#!/usr/bin/env python3
"""export_flow_logs.py — Bulk-export Amazon Connect contact flow logs from CloudWatch.

Fetches flow-execution events from the Connect log group for a given time window
and writes a JSON file in filter-log-events format, ready for scenario_from_logs.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

FLOWSIM_DIR = Path(__file__).parent
LOGS_DIR    = FLOWSIM_DIR / "Logs"

_CFG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

_MAN = """\
NAME
    export_flow_logs.py — Bulk-export Amazon Connect flow logs from CloudWatch

SYNOPSIS
    python export_flow_logs.py --instance-id UUID --region REGION [OPTIONS]

DESCRIPTION
    Fetches contact flow-execution events from the Connect CloudWatch log group
    (/aws/connect/<instance-alias>) for a specified time window and writes them
    to a JSON file in filter-log-events format.

    Output files are saved to flowSim/Logs/ by default, where they are
    automatically discovered by the flowsim CLI's scenario builder.

    Stops fetching once --max unique contacts have been seen (default 100).
    Use --max 0 to export all contacts in the window (may be slow on busy
    instances).

OPTIONS
    --instance-id UUID   (required)
        Amazon Connect instance UUID.

    --region REGION      (required)
        AWS region (e.g. us-east-1).

    --profile NAME
        AWS named profile for local development.

TIME RANGE (mutually exclusive; default is yesterday)
    --yesterday
        Previous calendar day in UTC (midnight to midnight).

    --last-week
        Previous 7 calendar days in UTC.

    --last DURATION
        Rolling window from now: 30m, 4h, 2d, 1w.

    --start DATETIME
        Start of window. Format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (UTC).

    --end DATETIME
        End of window (used with --start). Default: now.

OUTPUT OPTIONS
    --max N
        Stop after N unique contacts (default 100; 0 for unlimited).

    --out-dir DIR
        Directory to write the file (default: flowSim/Logs/).

    --output FILE
        Output filename (auto-generated from time range if not set).

    --list
        List contacts found without writing a file.

    --json
        Print raw events JSON to stdout instead of writing a file.

EXAMPLES
    # Export yesterday's logs (default)
    python export_flow_logs.py --instance-id <UUID> --region us-east-1

    # Last 4 hours, up to 50 contacts
    python export_flow_logs.py --instance-id <UUID> --region us-east-1 --last 4h --max 50

    # Last week, unlimited contacts
    python export_flow_logs.py --instance-id <UUID> --region us-east-1 --last-week --max 0

    # Specific date range
    python export_flow_logs.py --instance-id <UUID> --region us-east-1 \\
        --start 2026-03-01 --end 2026-03-08

    # Preview contacts without saving
    python export_flow_logs.py --instance-id <UUID> --region us-east-1 --list

IAM PERMISSIONS
    connect:DescribeInstance
    logs:FilterLogEvents on /aws/connect/<instance-alias>
"""


# ── Date/time helpers ─────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch_ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def _parse_dt(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date {s!r} — use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")


def _parse_last(s: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([mhdw])", s.strip().lower())
    if not m:
        raise ValueError(f"Cannot parse --last {s!r} — use Nm, Nh, Nd, or Nw (e.g. 4h, 2d)")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n),
            "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def _time_range(args: argparse.Namespace) -> tuple[datetime, datetime]:
    now = _utc_now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if args.last_week:
        return midnight - timedelta(weeks=1), midnight
    if args.last:
        delta = _parse_last(args.last)
        return now - delta, now
    if args.start:
        start = _parse_dt(args.start)
        end = _parse_dt(args.end) if args.end else now
        if end <= start:
            raise ValueError("--end must be after --start")
        return start, end
    # Default / --yesterday
    return midnight - timedelta(days=1), midnight


def _filename(start: datetime, end: datetime) -> str:
    s = start.strftime("%Y%m%d")
    if (end - start) < timedelta(hours=25):
        e = end.strftime("%Y%m%d_%H%M")
    else:
        e = end.strftime("%Y%m%d")
    return f"logs_{s}_to_{e}.json"


# ── AWS helpers ───────────────────────────────────────────────────────────────

def _session(profile: str | None, region: str | None) -> boto3.Session:
    return boto3.Session(profile_name=profile or None, region_name=region or None)


def _resolve_log_group(session: boto3.Session, instance_id: str) -> str:
    connect = session.client("connect", config=_CFG)
    resp = connect.describe_instance(InstanceId=instance_id)
    alias = resp["Instance"]["InstanceAlias"]
    return f"/aws/connect/{alias}"


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _contact_id(event: dict) -> str:
    try:
        return json.loads(event["message"]).get("ContactId", "")
    except Exception:
        return ""


def fetch_events(
    session: boto3.Session,
    log_group: str,
    start: datetime,
    end: datetime,
    max_contacts: int,
    contact_id: str | None = None,
) -> tuple[list[dict], set[str]]:
    """
    Page through filter_log_events and collect events.

    Stops paginating once max_contacts unique ContactIds have been seen
    (or the window is exhausted if max_contacts == 0).

    If contact_id is given, scopes the CloudWatch query to that CID only
    and ignores max_contacts.

    Returns (events, seen_contact_ids).
    Events that belong to contacts beyond the limit are excluded.
    """
    cw = session.client("logs", config=_CFG)
    kwargs: dict = {
        "logGroupName": log_group,
        "startTime": _epoch_ms(start),
        "endTime": _epoch_ms(end),
        "limit": 10000,
    }
    if contact_id:
        kwargs["filterPattern"] = f'{{ $.ContactId = "{contact_id}" }}'

    events: list[dict] = []
    seen: set[str] = set()
    page_num = 0

    while True:
        page_num += 1
        try:
            resp = cw.filter_log_events(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                print(f"\n  Log group not found: {log_group}", file=sys.stderr)
                sys.exit(1)
            raise

        raw = resp.get("events", [])
        for ev in raw:
            cid = _contact_id(ev)
            if cid:
                if cid not in seen:
                    if max_contacts > 0 and len(seen) >= max_contacts:
                        continue  # skip new contacts beyond limit
                    seen.add(cid)
            events.append({
                "timestamp":     ev.get("timestamp"),
                "message":       ev.get("message", ""),
                "logStreamName": ev.get("logStreamName", ""),
            })

        print(
            f"  Page {page_num}: {len(raw):,} events fetched, "
            f"{len(seen):,} unique contacts ...     ",
            end="\r",
        )

        token = resp.get("nextToken")
        if not token:
            break
        if max_contacts > 0 and len(seen) >= max_contacts:
            break  # hit our limit — stop early
        kwargs["nextToken"] = token

    print()  # clear \r line

    # Remove events belonging to contacts we didn't commit to
    if max_contacts > 0:
        events = [ev for ev in events if not _contact_id(ev) or _contact_id(ev) in seen]

    return events, seen


# ── List / preview ────────────────────────────────────────────────────────────

def print_list(events: list[dict], seen: set[str]) -> None:
    by_contact: dict[str, list[dict]] = {}
    for ev in events:
        cid = _contact_id(ev)
        if cid:
            by_contact.setdefault(cid, []).append(ev)

    print(f"\n  {len(by_contact)} contact(s):\n")
    for i, (cid, evs) in enumerate(by_contact.items()):
        if i >= 50:
            print(f"  … and {len(by_contact) - 50} more")
            break
        # Build flow sequence
        flows: list[str] = []
        seen_flows: set[str] = set()
        for ev in evs:
            try:
                fn = json.loads(ev["message"]).get("ContactFlowName", "")
                if fn and fn not in seen_flows:
                    flows.append(fn)
                    seen_flows.add(fn)
            except Exception:
                pass
        ts = evs[0].get("timestamp") or 0
        ts_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        flow_str = " → ".join(flows[:4]) + (" …" if len(flows) > 4 else "")
        print(f"  {cid[:8]}…  {ts_str}  {flow_str}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="export_flow_logs.py",
        description="Export Amazon Connect flow logs from CloudWatch to a JSON file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_MAN,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID",
                   help="Connect instance UUID")
    p.add_argument("--region",     default=None, metavar="REGION")
    p.add_argument("--profile",    default=None, metavar="PROFILE")
    p.add_argument("--contact-id", default=None, metavar="CID",
                   help="Filter to a single contact ID")

    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--yesterday",  action="store_true",
                     help="Previous calendar day UTC (default)")
    grp.add_argument("--last-week",  action="store_true",
                     help="Previous 7 calendar days UTC")
    grp.add_argument("--last",       metavar="DURATION",
                     help="Rolling window: 30m, 4h, 2d, 1w")
    grp.add_argument("--start",      metavar="DATETIME",
                     help="Start time YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (UTC)")

    p.add_argument("--end", metavar="DATETIME",
                   help="End time (with --start; default: now)")
    p.add_argument("--max", type=int, default=100, metavar="N",
                   help="Max unique contacts (default 100; 0 = unlimited)")
    p.add_argument("--out-dir", default=str(LOGS_DIR), metavar="DIR",
                   help=f"Output directory (default: {LOGS_DIR})")
    p.add_argument("--output", default=None, metavar="FILE",
                   help="Output filename (auto-generated if omitted)")
    p.add_argument("--list", action="store_true",
                   help="List contacts found; don't write a file")
    p.add_argument("--json", action="store_true",
                   help="Print events JSON to stdout")
    return p


def main() -> None:
    if "--help-full" in sys.argv:
        print(_MAN)
        sys.exit(0)

    p = build_parser()
    args = p.parse_args()

    try:
        start, end = _time_range(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"  Instance : {args.instance_id}")
    if args.contact_id:
        print(f"  Contact  : {args.contact_id}")
    print(f"  Window   : {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC")
    if not args.contact_id:
        print(f"  Max      : {args.max if args.max > 0 else 'unlimited'} contacts")
    print()

    session = _session(args.profile, args.region)

    print("  Resolving log group ...", end=" ", flush=True)
    try:
        log_group = _resolve_log_group(session, args.instance_id)
        print(log_group)
    except ClientError as exc:
        print(f"\n  Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print("  Fetching events ...")
    try:
        events, seen = fetch_events(session, log_group, start, end, args.max,
                                    contact_id=args.contact_id or None)
    except ClientError as exc:
        print(f"\n  Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"  Done — {len(events):,} events, {len(seen):,} unique contacts.")

    if not events:
        print("  No events found for this window.")
        sys.exit(0)

    if args.list:
        print_list(events, seen)
        return

    meta = {
        "instance_id":   args.instance_id,
        "log_group":     log_group,
        "start_time":    start.isoformat(),
        "end_time":      end.isoformat(),
        "contact_count": len(seen),
        "event_count":   len(events),
        "max_contacts":  args.max,
        "exported_at":   _utc_now().isoformat(),
    }

    output = {"events": events, "_meta": meta}

    if args.json:
        print(json.dumps(output, indent=2))
        return

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.output:
        out_path = (out_dir / args.output
                    if not Path(args.output).is_absolute()
                    else Path(args.output))
    else:
        out_path = out_dir / _filename(start, end)

    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    size_kb = out_path.stat().st_size // 1024
    print()
    print(f"  Saved → {out_path}  ({size_kb} KB)")
    print()
    print(f"  Build scenarios:")
    print(f"    python scenario_from_logs.py \"{out_path}\"")
    print(f"    python scenario_from_logs.py \"{out_path}\" --archetypes --instance-id {args.instance_id}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Interrupted.")
        sys.exit(0)
