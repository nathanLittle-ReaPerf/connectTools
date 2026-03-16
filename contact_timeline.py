#!/usr/bin/env python3
"""contact_timeline.py — Chronological event timeline for an Amazon Connect contact.

Stitches together contact metadata milestones, every flow block execution from
CloudWatch logs, Lambda invocations, and (optionally) Contact Lens transcript turns
into a single sorted timeline.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import NamedTuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import ct_snapshot

RETRY_CONFIG         = Config(retries={"max_attempts": 5, "mode": "adaptive"})
LENS_RETENTION_HOURS = 24

_MAN = """\
NAME
    contact_timeline.py — Chronological event timeline for an Amazon Connect contact

SYNOPSIS
    python contact_timeline.py --instance-id UUID --contact-id UUID [OPTIONS]

DESCRIPTION
    Stitches together contact metadata milestones, every flow block execution
    from CloudWatch Logs, Lambda invocations, and (optionally) Contact Lens
    transcript turns into a single sorted timeline. Each event is shown with
    a T+ offset from contact initiation so you can see exactly how long each
    step took. Use --json or --output to export the full timeline as JSON.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --contact-id UUID
        Contact UUID to build the timeline for. Required.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --log-group NAME
        Override the auto-discovered Connect CloudWatch log group.
        Default: /aws/connect/<instance-alias>.

    --transcript
        Include Contact Lens transcript turns as LENS events in the timeline.

    --json
        Print the timeline as JSON to stdout.

    --output FILE
        Write JSON timeline to a file.

EXAMPLES
    # Human-readable timeline
    python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

    # Include transcript turns
    python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --transcript

    # JSON output
    python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --json

    # Override log group and save to file
    python contact_timeline.py --instance-id <UUID> --contact-id <UUID> \\
        --log-group /aws/connect/my-instance --output timeline.json

IAM PERMISSIONS
    connect:DescribeContact
    connect:DescribeInstance
    connect:DescribeQueue
    connect:DescribeUser
    connect:ListRealtimeContactAnalysisSegments
    logs:FilterLogEvents

NOTES
    Flow logs are fetched from 2 minutes before initiation to 5 minutes after
    disconnect (or now if the contact is still active). If no flow logs are found
    for the contact ID, a warning is printed but the contact milestone events are
    still shown.
"""

_LAMBDA_MODULE_TYPES = {"InvokeExternalResource", "InvokeLambdaFunction"}

_FLOW_BLOCK_LABELS = {
    "PlayPrompt":                "Play prompt",
    "GetUserInput":              "Get input",
    "CheckAttribute":            "Check attribute",
    "CheckHoursOfOperation":     "Check hours",
    "CheckAgentStatus":          "Check agent status",
    "SetQueue":                  "Set queue",
    "SetAttributes":             "Set attributes",
    "UpdateContactAttributes":   "Update attributes",
    "Transfer":                  "Transfer",
    "Disconnect":                "Disconnect block",
    "Loop":                      "Loop",
    "SetRecordingBehavior":      "Set recording",
    "StartMediaStreaming":       "Start media stream",
    "StopMediaStreaming":        "Stop media stream",
    "SetContactFlow":            "Set contact flow",
    "SetEventHook":              "Set event hook",
    "Wait":                      "Wait",
    "CreateTask":                "Create task",
    "EndFlowExecution":          "End flow",
}


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Chronological event timeline for an Amazon Connect contact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --contact-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --contact-id <UUID> --transcript
  %(prog)s --instance-id <UUID> --contact-id <UUID> --json
  %(prog)s --instance-id <UUID> --contact-id <UUID> --log-group /aws/connect/my-instance
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--contact-id",  required=True, metavar="UUID")
    p.add_argument("--region",     default=None,  help="AWS region (defaults to session/CloudShell region)")
    p.add_argument("--profile",    default=None,  help="AWS named profile")
    p.add_argument("--log-group",  default=None,  metavar="NAME",
                   help="Override auto-discovered Connect log group")
    p.add_argument("--transcript", action="store_true",
                   help="Include Contact Lens transcript turns in the timeline")
    p.add_argument("--json",       action="store_true", dest="output_json",
                   help="Emit raw JSON (pipe-friendly)")
    p.add_argument("--output",     default=None, metavar="FILE",
                   help="Write JSON output to file")
    return p.parse_args()


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


# ── Data fetchers ─────────────────────────────────────────────────────────────

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


def fetch_queue_name(connect, instance_id, queue_id):
    try:
        return connect.describe_queue(InstanceId=instance_id, QueueId=queue_id)["Queue"]["Name"]
    except ClientError:
        return None


def fetch_agent_name(connect, instance_id, agent_id):
    try:
        info = connect.describe_user(InstanceId=instance_id, UserId=agent_id)["User"]["IdentityInfo"]
        return f"{info.get('FirstName', '')} {info.get('LastName', '')}".strip() or None
    except ClientError:
        return None


def filter_log_events(logs_client, log_group, filter_pattern, start_ms, end_ms):
    """Paginate FilterLogEvents. Returns [] on missing log group or error."""
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


# ── Contact Lens fetchers (from contact_inspect) ──────────────────────────────

def fetch_lens_voice(connect, instance_id, contact_id):
    segs, token = [], None
    while True:
        kwargs = dict(
            InstanceId=instance_id, ContactId=contact_id,
            OutputType="Raw",
            SegmentTypes=["TRANSCRIPT", "CATEGORIES", "ISSUES", "SENTIMENT"],
        )
        if token:
            kwargs["NextToken"] = token
        try:
            resp = connect.list_realtime_contact_analysis_segments_v2(**kwargs)
        except ClientError as e:
            return None, str(e)
        segs.extend(resp.get("Segments", []))
        token = resp.get("NextToken")
        if not token:
            return segs, None


def fetch_lens_chat(connect, instance_id, contact_id):
    segs, token = [], None
    while True:
        kwargs = dict(
            InstanceId=instance_id, ContactId=contact_id,
            OutputType="Raw",
            SegmentTypes=["TRANSCRIPT", "CATEGORIES", "ISSUES",
                          "EVENT", "ATTACHMENTS", "POST_CONTACT_SUMMARY"],
        )
        if token:
            kwargs["NextToken"] = token
        try:
            resp = connect.list_realtime_contact_analysis_segments_v2(**kwargs)
        except ClientError as e:
            return None, str(e)
        segs.extend(resp.get("Segments", []))
        token = resp.get("NextToken")
        if not token:
            return segs, None


def collect_lens(connect, instance_id, contact):
    channel = contact.get("Channel", "")
    end     = contact.get("DisconnectTimestamp")
    if end is not None:
        age_h = (dt.datetime.now(tz=dt.timezone.utc) - end).total_seconds() / 3600
        if age_h >= LENS_RETENTION_HOURS:
            return {"skipped": "Expired (>24h)"}
    if channel == "VOICE":
        segs, err = fetch_lens_voice(connect, instance_id, contact["Id"])
        return {"error": err} if err else {"segments": segs}
    if channel in ("CHAT", "EMAIL"):
        segs, err = fetch_lens_chat(connect, instance_id, contact["Id"])
        return {"error": err} if err else {"segments": segs}
    return {"skipped": f"Not supported for channel: {channel}"}


# ── Timeline event ────────────────────────────────────────────────────────────

class TimelineEvent(NamedTuple):
    ts:       dt.datetime   # absolute UTC
    offset_s: float         # seconds from contact initiation
    kind:     str           # CONTACT | FLOW | LAMBDA | LENS
    label:    str           # human-readable description
    detail:   str           # secondary info (flow name, result, etc.)
    raw:      dict          # original source data for JSON output


# ── Offset formatter ──────────────────────────────────────────────────────────

def fmt_offset(seconds: float) -> str:
    s = max(0, int(seconds))
    m, sec = divmod(s, 60)
    h, m   = divmod(m, 60)
    if h:
        return f"T+{h:02d}:{m:02d}:{sec:02d}"
    return f"T+{m:02d}:{sec:02d}"


# ── Event builders ────────────────────────────────────────────────────────────

def contact_milestones(contact, names) -> list:
    events   = []
    init_ts  = contact.get("InitiationTimestamp")
    if not init_ts:
        return events

    method  = contact.get("InitiationMethod", "?")
    channel = contact.get("Channel", "?")
    events.append(TimelineEvent(
        init_ts, 0.0, "CONTACT", "Contact initiated",
        f"{method}  {channel}", {"source": "DescribeContact"},
    ))

    qi = contact.get("QueueInfo") or {}
    if qi.get("EnqueueTimestamp"):
        offset = (qi["EnqueueTimestamp"] - init_ts).total_seconds()
        queue_label = names.get("queue") or qi.get("Id") or ""
        events.append(TimelineEvent(
            qi["EnqueueTimestamp"], offset, "CONTACT", "Entered queue",
            queue_label, {"source": "DescribeContact"},
        ))

    ai = contact.get("AgentInfo") or {}
    if ai.get("ConnectedToAgentTimestamp"):
        offset = (ai["ConnectedToAgentTimestamp"] - init_ts).total_seconds()
        agent_label = names.get("agent") or ai.get("Id") or ""
        events.append(TimelineEvent(
            ai["ConnectedToAgentTimestamp"], offset, "CONTACT", "Agent connected",
            agent_label, {"source": "DescribeContact"},
        ))

    disc_ts = contact.get("DisconnectTimestamp")
    if disc_ts:
        offset = (disc_ts - init_ts).total_seconds()
        reason = contact.get("DisconnectReason") or ""
        events.append(TimelineEvent(
            disc_ts, offset, "CONTACT", "Contact disconnected",
            reason, {"source": "DescribeContact"},
        ))

    return events


def _event_ts(ev: dict, fallback_ms: int) -> dt.datetime:
    ts_str = ev.get("Timestamp")
    if ts_str:
        try:
            return dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(fallback_ms / 1000, tz=dt.timezone.utc)


def flow_events(cw_events: list, init_ts: dt.datetime) -> list:
    """Parse all Connect flow log events into TimelineEvents."""
    result = []
    for ev in cw_events:
        msg = parse_message(ev["message"])
        if not isinstance(msg, dict):
            continue

        module_type = msg.get("ContactFlowModuleType")
        if not module_type:
            continue

        ts       = _event_ts(msg, ev["timestamp"])
        offset_s = (ts - init_ts).total_seconds()
        flow_name = msg.get("ContactFlowName") or ""

        if module_type in _LAMBDA_MODULE_TYPES:
            params = msg.get("Parameters", {})
            arn    = params.get("FunctionArn") or params.get("LambdaFunctionARN") or ""
            fn     = arn.split(":")[-1] if ":" in arn else arn
            ext    = msg.get("ExternalResults") or msg.get("ExternalResult")
            result_str = "Success" if ext else msg.get("Error") or "Unknown"
            detail = f"{result_str}  ·  {flow_name}" if flow_name else result_str
            result.append(TimelineEvent(ts, offset_s, "LAMBDA", fn, detail, msg))
        else:
            label  = _FLOW_BLOCK_LABELS.get(module_type, module_type)
            result.append(TimelineEvent(ts, offset_s, "FLOW", label, flow_name, msg))

    return result


def lens_events(lens_data: dict, init_ts: dt.datetime) -> list:
    """Extract transcript turns as TimelineEvents."""
    segs = lens_data.get("segments") or []
    result = []
    for seg in segs:
        t = seg.get("Transcript")
        if not t:
            continue

        # Prefer BeginOffsetMillis (voice); fall back to AbsoluteTime (chat)
        if "BeginOffsetMillis" in t:
            offset_ms = t["BeginOffsetMillis"]
            ts        = init_ts + dt.timedelta(milliseconds=offset_ms)
            offset_s  = offset_ms / 1000
        elif "AbsoluteTime" in t:
            try:
                ts = dt.datetime.fromisoformat(t["AbsoluteTime"].replace("Z", "+00:00"))
            except ValueError:
                continue
            offset_s = (ts - init_ts).total_seconds()
        else:
            continue

        role    = t.get("ParticipantRole", "?")
        content = t.get("Content", "")
        if len(content) > 80:
            content = content[:79] + "…"
        sent = t.get("Sentiment", "")

        label  = f"{role}: {content}"
        detail = f"[{sent}]" if sent else ""
        result.append(TimelineEvent(ts, offset_s, "LENS", label, detail, t))

    return result


def build_timeline(milestones, flow_evs, lens_evs, include_transcript) -> list:
    all_events = milestones + flow_evs
    if include_transcript:
        all_events += lens_evs
    return sorted(all_events, key=lambda e: e.ts)


# ── Human-readable output ─────────────────────────────────────────────────────

_KIND_W   = 7
_LABEL_W  = 28
_OFFSET_W = 9


def _hr():
    print("  " + "─" * 72)


def _fmt_dur(seconds):
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s" if m else f"{s}s"


def print_timeline(contact, names, timeline, contact_id, log_group, lens_available):
    start_ts = contact.get("InitiationTimestamp")
    end_ts   = contact.get("DisconnectTimestamp")
    duration = (end_ts - start_ts).total_seconds() if start_ts and end_ts else None

    _hr()
    print(f"  CONTACT TIMELINE   {contact_id}")
    _hr()

    # Summary line
    parts = [
        f"Channel: {contact.get('Channel', '?')}",
        f"Duration: {_fmt_dur(duration)}",
    ]
    if names.get("queue"):
        parts.append(f"Queue: {names['queue']}")
    if names.get("agent"):
        parts.append(f"Agent: {names['agent']}")
    print(f"  {'    '.join(parts)}")
    print(f"  Log group: {log_group}")

    if not lens_available:
        print(f"  \033[90m  (Contact Lens unavailable — transcript omitted)\033[0m")

    n_flow   = sum(1 for e in timeline if e.kind == "FLOW")
    n_lambda = sum(1 for e in timeline if e.kind == "LAMBDA")
    n_lens   = sum(1 for e in timeline if e.kind == "LENS")
    counts   = f"{n_flow} flow block(s), {n_lambda} Lambda invocation(s)"
    if n_lens:
        counts += f", {n_lens} transcript turn(s)"
    print(f"  {len(timeline)} events  ({counts})\n")

    # Column header
    print(f"  {'OFFSET':<{_OFFSET_W}}  {'KIND':<{_KIND_W}}  {'EVENT':<{_LABEL_W}}  DETAIL")
    print(f"  {'─'*_OFFSET_W}  {'─'*_KIND_W}  {'─'*_LABEL_W}  {'─'*30}")

    for ev in timeline:
        offset = fmt_offset(ev.offset_s)
        kind   = ev.kind
        label  = ev.label if len(ev.label) <= _LABEL_W else ev.label[:_LABEL_W - 1] + "…"
        detail = ev.detail or ""
        # Max detail width = terminal width minus fixed columns
        if len(detail) > 50:
            detail = detail[:49] + "…"

        # Subtle kind-based styling
        if kind == "CONTACT":
            kind_fmt  = f"\033[1m{kind:<{_KIND_W}}\033[0m"
            label_fmt = f"\033[1m{label:<{_LABEL_W}}\033[0m"
        elif kind == "LAMBDA":
            kind_fmt  = f"\033[33m{kind:<{_KIND_W}}\033[0m"
            label_fmt = f"\033[33m{label:<{_LABEL_W}}\033[0m"
        elif kind == "LENS":
            kind_fmt  = f"\033[90m{kind:<{_KIND_W}}\033[0m"
            label_fmt = f"\033[90m{label:<{_LABEL_W}}\033[0m"
            detail    = f"\033[90m{detail}\033[0m"
        else:
            kind_fmt  = f"{kind:<{_KIND_W}}"
            label_fmt = f"{label:<{_LABEL_W}}"

        print(f"  {offset:<{_OFFSET_W}}  {kind_fmt}  {label_fmt}  {detail}")

    _hr()
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def _ms(ts: dt.datetime) -> int:
    return int(ts.timestamp() * 1000)


def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    args           = parse_args()
    connect, logs_client = make_clients(args.region, args.profile)

    contact  = fetch_contact(connect, args.instance_id, args.contact_id)
    start_ts = contact.get("InitiationTimestamp")
    end_ts   = contact.get("DisconnectTimestamp")

    if start_ts is None:
        print("Error: contact has no InitiationTimestamp.", file=sys.stderr)
        sys.exit(1)

    # Resolve names
    names = {}
    qi = contact.get("QueueInfo") or {}
    if qi.get("Id"):
        names["queue"] = fetch_queue_name(connect, args.instance_id, qi["Id"])
    ai = contact.get("AgentInfo") or {}
    if ai.get("Id"):
        names["agent"] = fetch_agent_name(connect, args.instance_id, ai["Id"])

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
    print(f"  Fetching flow logs...", file=sys.stderr)

    now      = dt.datetime.now(dt.timezone.utc)
    start_ms = _ms(start_ts - dt.timedelta(minutes=2))
    end_ms   = _ms(min(end_ts + dt.timedelta(minutes=5), now) if end_ts else now)

    cw_events = filter_log_events(
        logs_client, log_group,
        f'{{ $.ContactId = "{args.contact_id}" }}',
        start_ms, end_ms,
    )

    if not cw_events:
        # Distinguish "flow logging not enabled / log group empty" from "contact not in logs"
        probe = filter_log_events(logs_client, log_group, "", start_ms, end_ms)
        if not probe:
            print(
                f"  Warning: no flow log events found in {log_group} for the contact's time window.\n"
                f"           Flow logging may not be enabled on this instance.\n"
                f"           Check: Connect console → Instance → Data storage → Flow logs.",
                file=sys.stderr,
            )
        else:
            print(
                f"  Warning: flow logs exist for this time window but contact {args.contact_id}\n"
                f"           was not found. The flows this contact traversed may not have\n"
                f"           flow logging enabled (set per-flow in the flow editor).",
                file=sys.stderr,
            )

    # Contact Lens
    lens_data = {"skipped": "not requested"}
    if args.transcript or args.output_json or args.output:
        print(f"  Fetching Contact Lens...", file=sys.stderr)
        lens_data = collect_lens(connect, args.instance_id, contact)

    lens_available = "segments" in lens_data

    # Build event lists
    milestones = contact_milestones(contact, names)
    flow_evs   = flow_events(cw_events, start_ts)
    lens_evs   = lens_events(lens_data, start_ts) if lens_available else []

    timeline = build_timeline(milestones, flow_evs, lens_evs, args.transcript)

    if args.output_json or args.output:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)

        doc = {
            "contact_id":     args.contact_id,
            "contact":        contact,
            "names":          names,
            "log_group":      log_group,
            "lens_available": lens_available,
            "event_count":    len(timeline),
            "events": [
                {
                    "offset_s":   round(e.offset_s, 3),
                    "offset_fmt": fmt_offset(e.offset_s),
                    "timestamp":  e.ts.isoformat(),
                    "kind":       e.kind,
                    "label":      e.label,
                    "detail":     e.detail,
                }
                for e in timeline
            ],
            "raw": {
                "flow_log_events":   len(cw_events),
                "lens_segments":     len(lens_data.get("segments") or []),
            },
        }
        out = json.dumps(doc, indent=2, default=serial)
        if args.output:
            dest = ct_snapshot.output_path("contact_timeline", args.output)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"  Saved → {dest}", file=sys.stderr)
        else:
            print(out)
    else:
        print_timeline(contact, names, timeline, args.contact_id, log_group, lens_available)


if __name__ == "__main__":
    main()
