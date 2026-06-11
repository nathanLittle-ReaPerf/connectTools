#!/usr/bin/env python3
"""contact_investigator.py — Unified Amazon Connect contact investigation.

Consolidates contact_inspect, contact_timeline, lambda_tracer,
contact_recordings, and contact_logs into a single tool. Any combination of
sections can be run in one invocation. Shared API calls (DescribeContact,
CloudWatch log fetch, Contact Lens) are made once and reused across sections.
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

import ct_config
import ct_snapshot

RETRY_CONFIG         = Config(retries={"max_attempts": 5, "mode": "adaptive"})
LENS_RETENTION_HOURS = 24
LAMBDA_WINDOW_SECS   = 30
REFERENCE_TYPES      = ["URL", "ATTACHMENT", "CONTACT_ANALYSIS", "NUMBER", "STRING", "DATE"]
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

_MAN = """\
NAME
    contact_investigator.py — Unified Amazon Connect contact investigation

SYNOPSIS
    python contact_investigator.py --instance-id UUID --contact-id UUID [SECTIONS] [OPTIONS]

DESCRIPTION
    Runs any combination of investigation sections for a single contact ID.
    Shared API calls (DescribeContact, CloudWatch log fetch, Contact Lens) are
    made once and reused across sections, so --all is not slower than running
    each tool individually.

    Default sections (when none specified): --overview --timeline

SECTIONS
    --overview       Contact metadata, custom attributes, references, transfer
                     chain, and Contact Lens summary.
    --timeline       Chronological event timeline: contact milestones, flow block
                     executions, Lambda invocations, and (with --transcript)
                     Lens transcript turns.
    --lambda         Lambda invocation trace: ARN, result, Connect-side response.
                     Add --lambda-logs to also fetch each function's CloudWatch logs.
    --recordings     S3 paths and presigned download URLs for recordings and
                     transcripts (original + redacted).
    --logs           Download raw CloudWatch flow-execution logs to a JSON or
                     text file.
    --all            Run all sections.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --contact-id UUID
        Contact UUID to investigate. Required.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --log-group NAME
        Override the auto-discovered Connect CloudWatch log group.
        Default: /aws/connect/<instance-alias>.

    --transcript
        Include Contact Lens transcript turns (--overview and --timeline sections).

    --lambda-logs
        Also fetch each Lambda function's CloudWatch logs (--lambda section only).
        Without this flag only invocation metadata and Connect-side responses are shown.

    --url-expires SECONDS
        Presigned URL expiry for --recordings. Default: 3600 (1 hour).

    --json
        Emit all sections as a single JSON document (pipe-friendly).

    --output FILE
        Write JSON output to a file.

EXAMPLES
    # Default: overview + timeline
    python contact_investigator.py --instance-id <UUID> --contact-id <UUID>

    # Full investigation
    python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --all --transcript

    # Lambda trace with CloudWatch logs
    python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --lambda --lambda-logs

    # Recordings only with 2-hour URLs
    python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --recordings --url-expires 7200

    # JSON of all sections
    python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --all --json | jq '.overview.contact.Channel'

    # Download raw flow logs
    python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --logs

IAM PERMISSIONS
    connect:DescribeContact                       (all sections)
    connect:GetContactAttributes                  (--overview)
    connect:ListContactReferences                 (--overview)
    connect:DescribeQueue                         (--overview, --timeline)
    connect:DescribeUser                          (--overview, --timeline)
    connect:ListRealtimeContactAnalysisSegmentsV2 (--overview, --timeline with --transcript)
    connect:DescribeInstance                      (--timeline, --lambda, --logs)
    logs:FilterLogEvents on /aws/connect/*        (--timeline, --lambda, --logs)
    logs:FilterLogEvents on /aws/lambda/*         (--lambda with --lambda-logs)
    connect:ListInstanceStorageConfigs            (--recordings)
    s3:ListBucket, s3:GetObject                   (--recordings)
"""


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Unified Amazon Connect contact investigation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
sections (default: --overview --timeline):
  --overview    metadata, attributes, Lens summary
  --timeline    chronological event timeline
  --lambda      Lambda invocation trace
  --recordings  S3 recordings / transcripts with presigned URLs
  --logs        download raw CloudWatch flow-execution logs

examples:
  %(prog)s --instance-id <UUID> --contact-id <UUID>
  %(prog)s --instance-id <UUID> --contact-id <UUID> --all --transcript
  %(prog)s --instance-id <UUID> --contact-id <UUID> --lambda --lambda-logs
  %(prog)s --instance-id <UUID> --contact-id <UUID> --recordings --url-expires 7200
  %(prog)s --instance-id <UUID> --contact-id <UUID> --all --json | jq '.timeline.event_count'
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--contact-id",  required=True, metavar="UUID")
    p.add_argument("--region",      default=None,  help="AWS region")
    p.add_argument("--profile",     default=None,  help="AWS named profile")
    p.add_argument("--log-group",   default=None,  metavar="NAME",
                   help="Override auto-discovered Connect log group")

    sec = p.add_argument_group("sections")
    sec.add_argument("--overview",    action="store_true", help="Contact metadata, attributes, Lens summary")
    sec.add_argument("--timeline",    action="store_true", help="Chronological event timeline")
    sec.add_argument("--lambda",      action="store_true", dest="lambda_trace", help="Lambda invocation trace")
    sec.add_argument("--recordings",  action="store_true", help="S3 recordings and transcripts")
    sec.add_argument("--logs",        action="store_true", help="Download raw CloudWatch flow logs")
    sec.add_argument("--all",         action="store_true", help="Run all sections")

    p.add_argument("--transcript",   action="store_true", help="Include Lens transcript turns (overview + timeline)")
    p.add_argument("--lambda-logs",  action="store_true", dest="lambda_logs",
                   help="Fetch Lambda CloudWatch logs (--lambda section)")
    p.add_argument("--url-expires",  type=int, default=3600, metavar="SECS",
                   help="Presigned URL expiry for --recordings (default: 3600)")
    p.add_argument("--json",         action="store_true", dest="output_json", help="Emit JSON output")
    p.add_argument("--output",       default=None, metavar="FILE", help="Write JSON to file")

    args = p.parse_args()

    if args.all:
        args.overview = args.timeline = args.lambda_trace = args.recordings = args.logs = True
    elif not any([args.overview, args.timeline, args.lambda_trace, args.recordings, args.logs]):
        args.overview = args.timeline = True

    return args


# ── Client factory ────────────────────────────────────────────────────────────

def make_clients(region, profile, need_logs=False, need_s3=False):
    """Returns (connect, logs_client_or_None, s3_client_or_None)."""
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    connect     = session.client("connect", region_name=resolved, config=RETRY_CONFIG)
    logs_client = session.client("logs", region_name=resolved, config=RETRY_CONFIG) if need_logs else None
    s3_client   = session.client("s3",   region_name=resolved, config=RETRY_CONFIG) if need_s3  else None
    return connect, logs_client, s3_client


# ── Shared fetchers ───────────────────────────────────────────────────────────

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


def resolve_log_group(connect, instance_id, override=None):
    """Return the Connect log group, persisting to ct_config. Exits on failure."""
    if override:
        cfg = ct_config.load()
        ct_config.set_log_group(cfg, instance_id, override)
        return override
    cached = ct_config.get_log_group(instance_id)
    if cached:
        return cached
    alias = fetch_instance_alias(connect, instance_id)
    if alias:
        return f"/aws/connect/{alias}"
    print(
        "Error: could not auto-discover Connect log group.\n"
        "Pass --log-group /aws/connect/<your-instance-alias> explicitly.",
        file=sys.stderr,
    )
    sys.exit(1)


def filter_log_events(logs_client, log_group, filter_pattern, start_ms, end_ms):
    """Paginate FilterLogEvents. Returns [] on missing log group or error."""
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


def _ms(ts: dt.datetime) -> int:
    return int(ts.timestamp() * 1000)


def _parse_message(raw: str) -> dict:
    try:
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return {"raw": raw.strip()}


# ── Contact Lens helpers ──────────────────────────────────────────────────────

def lens_age_hours(contact):
    end = contact.get("DisconnectTimestamp")
    if end is None:
        return None
    return (dt.datetime.now(tz=dt.timezone.utc) - end).total_seconds() / 3600


def _fetch_lens_voice(connect, instance_id, contact_id):
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


def _fetch_lens_chat(connect, instance_id, contact_id):
    segs, token, status = [], None, None
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
            return None, None, str(e)
        segs.extend(resp.get("Segments", []))
        status = resp.get("Status")
        token  = resp.get("NextToken")
        if not token:
            return segs, status, None


def collect_lens(connect, instance_id, contact):
    """
    Returns one of:
      {"skipped": "reason"}               — outside window or unsupported channel
      {"error": "message"}                — API failure
      {"segments": [...]}                 — voice success
      {"segments": [...], "status": str}  — chat success
    """
    channel = contact.get("Channel", "")
    age_h   = lens_age_hours(contact)
    if age_h is not None and age_h >= LENS_RETENTION_HOURS:
        return {
            "skipped": (
                f"Data expired ({int(age_h)}h old; {LENS_RETENTION_HOURS}h retention limit). "
                "Check your S3 export bucket if Contact Lens output is configured there."
            )
        }
    if channel == "VOICE":
        segs, err = _fetch_lens_voice(connect, instance_id, contact["Id"])
        return {"error": err} if err else {"segments": segs}
    if channel in ("CHAT", "EMAIL"):
        segs, status, err = _fetch_lens_chat(connect, instance_id, contact["Id"])
        if err:
            return {"error": err}
        result = {"segments": segs}
        if status:
            result["status"] = status
        return result
    return {"skipped": f"Contact Lens not supported for channel: {channel}"}


# ── Shared name resolvers ─────────────────────────────────────────────────────

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


def resolve_names(connect, instance_id, contact):
    names = {}
    qi = contact.get("QueueInfo") or {}
    if qi.get("Id"):
        names["queue"] = fetch_queue_name(connect, instance_id, qi["Id"])
    ai = contact.get("AgentInfo") or {}
    if ai.get("Id"):
        names["agent"] = fetch_agent_name(connect, instance_id, ai["Id"])
    return names


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_ts(ts):
    if ts is None:
        return "—"
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(ts)


def _fmt_dur(seconds):
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s" if m else f"{s}s"


def _section(title):
    print(f"\n{'─' * 64}")
    print(f"  {title}")
    print(f"{'─' * 64}")


def _row(label, value, indent=2):
    pad = " " * indent
    print(f"{pad}{label:<28} {value}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Overview
# ══════════════════════════════════════════════════════════════════════════════

def fetch_attributes(connect, instance_id, contact_id):
    try:
        return connect.get_contact_attributes(
            InstanceId=instance_id, InitialContactId=contact_id
        ).get("Attributes", {})
    except ClientError as e:
        return {"_error": str(e)}


def fetch_references(connect, instance_id, contact_id):
    refs, token = [], None
    while True:
        kwargs = dict(
            InstanceId=instance_id,
            ContactId=contact_id,
            ReferenceTypes=REFERENCE_TYPES,
        )
        if token:
            kwargs["NextToken"] = token
        try:
            resp = connect.list_contact_references(**kwargs)
        except ClientError as e:
            return [{"_error": str(e)}]
        refs.extend(resp.get("ReferenceSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return refs


def fetch_transfer_chain(connect, instance_id, contact):
    chain, seen = [], {contact["Id"]}
    current = contact
    while True:
        prev_id = current.get("PreviousContactId")
        if not prev_id or prev_id in seen:
            break
        seen.add(prev_id)
        try:
            prev = connect.describe_contact(InstanceId=instance_id, ContactId=prev_id)["Contact"]
            chain.insert(0, prev)
            current = prev
        except ClientError:
            break
    return chain


def print_overview(contact, attributes, references, chain, lens_data, names, show_transcript):
    _section(f"OVERVIEW  {contact.get('Id', '?')}")

    start = contact.get("InitiationTimestamp")
    end   = contact.get("DisconnectTimestamp")
    dur   = (end - start).total_seconds() if start and end else None

    _row("Channel:",              contact.get("Channel", "?"))
    _row("Initiation method:",    contact.get("InitiationMethod", "?"))
    _row("Initiated:",            _fmt_ts(start))
    _row("Disconnected:",         _fmt_ts(end))
    if contact.get("DisconnectReason"):
        _row("Disconnect reason:", contact["DisconnectReason"])
    _row("Duration:",             _fmt_dur(dur))

    qi = contact.get("QueueInfo") or {}
    if qi.get("Id"):
        _row("Queue:",            names.get("queue") or qi["Id"])
        _row("Enqueued:",         _fmt_ts(qi.get("EnqueueTimestamp")))

    ai = contact.get("AgentInfo") or {}
    if ai.get("Id"):
        _row("Agent:",            names.get("agent") or ai["Id"])
        _row("Agent connected:",  _fmt_ts(ai.get("ConnectedToAgentTimestamp")))
        if ai.get("AgentPauseDurationInSeconds"):
            _row("Agent pause total:", _fmt_dur(ai["AgentPauseDurationInSeconds"]))

    ce = contact.get("CustomerEndpoint") or {}
    if ce.get("Address"):
        _row("Customer endpoint:", f"{ce['Address']} ({ce.get('Type', '?')})")

    if chain:
        print(f"\n  Transfer chain (oldest → current):")
        for c in chain:
            print(f"    {c.get('Id', '?')[:8]}…  ({c.get('Channel', '?')})  "
                  f"{_fmt_ts(c.get('InitiationTimestamp'))}")
        print(f"    ↳ current: {contact['Id']}")

    if attributes:
        _section("CONTACT ATTRIBUTES")
        if "_error" in attributes:
            print(f"  Error: {attributes['_error']}")
        else:
            for k, v in sorted(attributes.items()):
                print(f"  {k:<32} {v}")

    if references:
        _section("REFERENCES")
        for ref in references:
            if "_error" in ref:
                print(f"  Error: {ref['_error']}")
                continue
            for rtype, detail in ref.items():
                val = detail.get("Value") or detail.get("Url") or str(detail) if isinstance(detail, dict) else str(detail)
                _row(f"  [{rtype}]", val)

    _section("CONTACT LENS")

    if "skipped" in lens_data:
        print(f"  {lens_data['skipped']}")
        return
    if "error" in lens_data:
        print(f"  Unavailable: {lens_data['error']}")
        return

    segs = lens_data.get("segments")
    if not segs:
        print("  No data.")
        return

    if lens_data.get("status"):
        _row("Analysis status:", lens_data["status"])

    transcripts = [s["Transcript"] for s in segs if "Transcript" in s]
    categories  = [s["Categories"]  for s in segs if "Categories"  in s]
    summaries   = [s["PostContactSummary"] for s in segs if "PostContactSummary" in s]

    _row("Transcript turns:", str(len(transcripts)))

    role_sentiments: dict = {}
    for t in transcripts:
        role = t.get("ParticipantRole", "UNKNOWN")
        sent = t.get("Sentiment")
        if sent:
            role_sentiments.setdefault(role, []).append(sent)

    SENT_SYM = {"POSITIVE": "+", "NEUTRAL": "=", "NEGATIVE": "-"}
    for role, sent_list in sorted(role_sentiments.items()):
        counts   = {s: sent_list.count(s) for s in ["POSITIVE", "NEUTRAL", "NEGATIVE"]}
        dominant = max(counts, key=counts.get)
        detail   = "  ".join(f"{SENT_SYM[k]}{counts[k]}" for k in ["POSITIVE", "NEUTRAL", "NEGATIVE"])
        _row(f"  {role} sentiment:", f"{dominant}  ({detail})")

    all_issues = []
    for t in transcripts:
        for issue in t.get("Issues", []):
            txt = issue.get("IssueDetected", {}).get("Text") or issue.get("Text", "")
            if txt:
                all_issues.append(f'"{txt}"')
    if all_issues:
        _row("Issues detected:", ", ".join(all_issues))

    for cat in categories:
        matched = cat.get("MatchedCategories", [])
        if matched:
            _row("Categories:", ", ".join(matched))

    for pcs in summaries:
        content = pcs.get("Content", "")
        if content:
            print("\n  Post-contact summary:")
            for line in content.splitlines():
                print(f"    {line}")

    if show_transcript and transcripts:
        print("\n  Transcript:")
        for t in transcripts:
            role    = t.get("ParticipantRole", "?")
            content = t.get("Content", "")
            begin_ms = t.get("BeginOffsetMillis", 0)
            m, s = divmod(begin_ms // 1000, 60)
            print(f"    [{m:02d}:{s:02d}] {role}: {content}")
    elif transcripts and not show_transcript:
        print(f"\n  ({len(transcripts)} turns — use --transcript to print)")


def run_overview(connect, instance_id, contact_id, contact, lens_cache, names,
                 show_transcript, output_json):
    attributes = fetch_attributes(connect, instance_id, contact_id)
    references = fetch_references(connect, instance_id, contact_id)
    chain      = fetch_transfer_chain(connect, instance_id, contact)

    if "data" not in lens_cache:
        print("  Fetching Contact Lens...", file=sys.stderr)
        lens_cache["data"] = collect_lens(connect, instance_id, contact)
    lens_data = lens_cache["data"]

    if not output_json:
        print_overview(contact, attributes, references, chain, lens_data, names, show_transcript)

    return {
        "contact":        contact,
        "attributes":     attributes,
        "references":     references,
        "transfer_chain": chain,
        "contact_lens":   lens_data,
        "names":          names,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Timeline
# ══════════════════════════════════════════════════════════════════════════════

class TimelineEvent(NamedTuple):
    ts:       dt.datetime
    offset_s: float
    kind:     str
    label:    str
    detail:   str
    raw:      dict


def _fmt_offset(seconds: float) -> str:
    s = max(0, int(seconds))
    m, sec = divmod(s, 60)
    h, m   = divmod(m, 60)
    if h:
        return f"T+{h:02d}:{m:02d}:{sec:02d}"
    return f"T+{m:02d}:{sec:02d}"


def _event_ts(ev: dict, fallback_ms: int) -> dt.datetime:
    ts_str = ev.get("Timestamp")
    if ts_str:
        try:
            return dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(fallback_ms / 1000, tz=dt.timezone.utc)


def build_contact_milestones(contact, names) -> list:
    events  = []
    init_ts = contact.get("InitiationTimestamp")
    if not init_ts:
        return events
    method  = contact.get("InitiationMethod", "?")
    channel = contact.get("Channel", "?")
    events.append(TimelineEvent(init_ts, 0.0, "CONTACT", "Contact initiated",
                                f"{method}  {channel}", {"source": "DescribeContact"}))
    qi = contact.get("QueueInfo") or {}
    if qi.get("EnqueueTimestamp"):
        offset = (qi["EnqueueTimestamp"] - init_ts).total_seconds()
        events.append(TimelineEvent(qi["EnqueueTimestamp"], offset, "CONTACT", "Entered queue",
                                    names.get("queue") or qi.get("Id") or "",
                                    {"source": "DescribeContact"}))
    ai = contact.get("AgentInfo") or {}
    if ai.get("ConnectedToAgentTimestamp"):
        offset = (ai["ConnectedToAgentTimestamp"] - init_ts).total_seconds()
        events.append(TimelineEvent(ai["ConnectedToAgentTimestamp"], offset, "CONTACT", "Agent connected",
                                    names.get("agent") or ai.get("Id") or "",
                                    {"source": "DescribeContact"}))
    disc_ts = contact.get("DisconnectTimestamp")
    if disc_ts:
        offset = (disc_ts - init_ts).total_seconds()
        events.append(TimelineEvent(disc_ts, offset, "CONTACT", "Contact disconnected",
                                    contact.get("DisconnectReason") or "",
                                    {"source": "DescribeContact"}))
    return events


def build_flow_events(cw_events: list, init_ts: dt.datetime) -> list:
    result = []
    for ev in cw_events:
        msg = _parse_message(ev["message"])
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
            label = _FLOW_BLOCK_LABELS.get(module_type, module_type)
            result.append(TimelineEvent(ts, offset_s, "FLOW", label, flow_name, msg))
    return result


def build_lens_events(lens_data: dict, init_ts: dt.datetime) -> list:
    segs = lens_data.get("segments") or []
    result = []
    for seg in segs:
        t = seg.get("Transcript")
        if not t:
            continue
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
        sent   = t.get("Sentiment", "")
        result.append(TimelineEvent(ts, offset_s, "LENS",
                                    f"{role}: {content}",
                                    f"[{sent}]" if sent else "", t))
    return result


def print_timeline(contact, names, timeline, contact_id, log_group, lens_available):
    start_ts = contact.get("InitiationTimestamp")
    end_ts   = contact.get("DisconnectTimestamp")
    dur = (end_ts - start_ts).total_seconds() if start_ts and end_ts else None

    _section(f"TIMELINE  {contact_id}")
    parts = [f"Channel: {contact.get('Channel', '?')}", f"Duration: {_fmt_dur(dur)}"]
    if names.get("queue"):
        parts.append(f"Queue: {names['queue']}")
    if names.get("agent"):
        parts.append(f"Agent: {names['agent']}")
    print(f"  {'    '.join(parts)}")
    print(f"  Log group: {log_group}")
    if not lens_available:
        print(f"  \033[90m(Contact Lens unavailable — transcript omitted)\033[0m")

    n_flow   = sum(1 for e in timeline if e.kind == "FLOW")
    n_lambda = sum(1 for e in timeline if e.kind == "LAMBDA")
    n_lens   = sum(1 for e in timeline if e.kind == "LENS")
    counts   = f"{n_flow} flow block(s), {n_lambda} Lambda invocation(s)"
    if n_lens:
        counts += f", {n_lens} transcript turn(s)"
    print(f"  {len(timeline)} events  ({counts})\n")

    OW, KW, LW = 9, 7, 28
    print(f"  {'OFFSET':<{OW}}  {'KIND':<{KW}}  {'EVENT':<{LW}}  DETAIL")
    print(f"  {'─'*OW}  {'─'*KW}  {'─'*LW}  {'─'*30}")

    for ev in timeline:
        offset = _fmt_offset(ev.offset_s)
        label  = ev.label if len(ev.label) <= LW else ev.label[:LW - 1] + "…"
        detail = ev.detail[:50] + "…" if len(ev.detail) > 50 else ev.detail
        if ev.kind == "CONTACT":
            kf = f"\033[1m{ev.kind:<{KW}}\033[0m"
            lf = f"\033[1m{label:<{LW}}\033[0m"
        elif ev.kind == "LAMBDA":
            kf = f"\033[33m{ev.kind:<{KW}}\033[0m"
            lf = f"\033[33m{label:<{LW}}\033[0m"
        elif ev.kind == "LENS":
            kf = f"\033[90m{ev.kind:<{KW}}\033[0m"
            lf = f"\033[90m{label:<{LW}}\033[0m"
            detail = f"\033[90m{detail}\033[0m"
        else:
            kf = f"{ev.kind:<{KW}}"
            lf = f"{label:<{LW}}"
        print(f"  {offset:<{OW}}  {kf}  {lf}  {detail}")

    print("  " + "─" * 72)


def run_timeline(connect, logs_client, instance_id, contact_id, contact,
                 log_group, cw_events, lens_cache, names, show_transcript, output_json):
    start_ts = contact.get("InitiationTimestamp")

    if "data" not in lens_cache and (show_transcript or output_json):
        print("  Fetching Contact Lens...", file=sys.stderr)
        lens_cache["data"] = collect_lens(connect, instance_id, contact)

    lens_data      = lens_cache.get("data", {"skipped": "not requested"})
    lens_available = "segments" in lens_data

    milestones = build_contact_milestones(contact, names)
    flow_evs   = build_flow_events(cw_events, start_ts) if start_ts else []
    lens_evs   = build_lens_events(lens_data, start_ts) if (lens_available and start_ts) else []

    all_events = milestones + flow_evs
    if show_transcript:
        all_events += lens_evs
    timeline = sorted(all_events, key=lambda e: e.ts)

    if not output_json:
        print_timeline(contact, names, timeline, contact_id, log_group, lens_available)

    return {
        "contact_id":     contact_id,
        "log_group":      log_group,
        "lens_available": lens_available,
        "event_count":    len(timeline),
        "flow_log_count": len(cw_events),
        "events": [
            {
                "offset_s":   round(e.offset_s, 3),
                "offset_fmt": _fmt_offset(e.offset_s),
                "timestamp":  e.ts.isoformat(),
                "kind":       e.kind,
                "label":      e.label,
                "detail":     e.detail,
            }
            for e in timeline
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Lambda trace
# ══════════════════════════════════════════════════════════════════════════════

def extract_lambda_invocations(cw_events: list) -> list:
    invocations = []
    for ev in cw_events:
        msg = _parse_message(ev["message"])
        if not isinstance(msg, dict):
            continue
        if msg.get("ContactFlowModuleType") not in _LAMBDA_MODULE_TYPES:
            continue
        params = msg.get("Parameters", {})
        arn    = params.get("FunctionArn") or params.get("LambdaFunctionARN") or ""
        if not arn:
            continue
        fn     = arn.split(":")[-1] if ":" in arn else arn
        ts_str = msg.get("Timestamp")
        if ts_str:
            try:
                invoked_at = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                invoked_at = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, tz=dt.timezone.utc)
        else:
            invoked_at = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, tz=dt.timezone.utc)
        external = msg.get("ExternalResults") or msg.get("ExternalResult")
        result   = "Success" if external else msg.get("Error", "Unknown")
        invocations.append({
            "function_arn":     arn,
            "function_name":    fn,
            "invoked_at":       invoked_at,
            "result":           result,
            "connect_response": external,
            "flow_name":        msg.get("ContactFlowName"),
        })
    return invocations


def fetch_lambda_logs(logs_client, function_name: str, invoked_at: dt.datetime) -> list:
    log_group = f"/aws/lambda/{function_name}"
    window    = dt.timedelta(seconds=LAMBDA_WINDOW_SECS)
    raw = filter_log_events(logs_client, log_group, "",
                            _ms(invoked_at - window), _ms(invoked_at + window))
    return [
        {
            "timestamp": dt.datetime.fromtimestamp(
                e["timestamp"] / 1000, tz=dt.timezone.utc
            ).isoformat(),
            "message": e["message"].rstrip(),
        }
        for e in raw
    ]


def print_lambda_trace(contact_id, invocations_with_logs, fetch_logs):
    _section(f"LAMBDA TRACE  {contact_id}")
    if not invocations_with_logs:
        print("\n  No Lambda invocations found in Connect flow logs.\n")
        return
    print(f"\n  {len(invocations_with_logs)} invocation(s) found.\n")
    for i, item in enumerate(invocations_with_logs, 1):
        inv  = item["invocation"]
        logs = item["lambda_logs"]
        print(f"  [{i}] {inv['function_name']}")
        print(f"       ARN      : {inv['function_arn']}")
        print(f"       Invoked  : {inv['invoked_at'].strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} UTC")
        print(f"       Flow     : {inv['flow_name'] or '(unknown)'}")
        print(f"       Result   : {inv['result']}")
        if inv["connect_response"]:
            resp_str = json.dumps(inv["connect_response"], separators=(",", ":"))
            if len(resp_str) > 120:
                resp_str = resp_str[:117] + "..."
            print(f"       Response : {resp_str}")
        if fetch_logs:
            print(f"\n       Lambda logs (±{LAMBDA_WINDOW_SECS}s window):")
            if logs:
                for entry in logs:
                    ts  = entry["timestamp"][11:23]
                    msg = entry["message"]
                    if len(msg) > 200:
                        msg = msg[:197] + "..."
                    print(f"         {ts}  {msg}")
            else:
                print(f"         (no events — check IAM or /aws/lambda/{inv['function_name']})")
        print()


def run_lambda(logs_client, contact_id, cw_events, fetch_logs, output_json):
    invocations = extract_lambda_invocations(cw_events)
    invocations_with_logs = []
    seen_functions: set = set()
    for inv in invocations:
        fn = inv["function_name"]
        lambda_logs = []
        if fetch_logs:
            if fn not in seen_functions:
                seen_functions.add(fn)
                print(f"    Fetching Lambda logs: /aws/lambda/{fn}", file=sys.stderr)
            lambda_logs = fetch_lambda_logs(logs_client, fn, inv["invoked_at"])
        invocations_with_logs.append({"invocation": inv, "lambda_logs": lambda_logs})

    if not output_json:
        print_lambda_trace(contact_id, invocations_with_logs, fetch_logs)

    def _serial(o):
        return o.isoformat() if hasattr(o, "isoformat") else str(o)

    return {
        "invocation_count": len(invocations),
        "lambda_logs_fetched": fetch_logs,
        "invocations": [
            {
                "function_arn":     item["invocation"]["function_arn"],
                "function_name":    item["invocation"]["function_name"],
                "invoked_at":       item["invocation"]["invoked_at"].isoformat(),
                "result":           item["invocation"]["result"],
                "connect_response": item["invocation"]["connect_response"],
                "flow_name":        item["invocation"]["flow_name"],
                "lambda_log_count": len(item["lambda_logs"]),
                "lambda_logs":      item["lambda_logs"],
            }
            for item in invocations_with_logs
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Recordings
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_storage_configs(connect, instance_id, resource_type):
    try:
        resp = connect.list_instance_storage_configs(
            InstanceId=instance_id,
            ResourceType=resource_type,
        )
        return [
            sc["S3Config"]
            for sc in resp.get("StorageConfigs", [])
            if sc.get("StorageType") == "S3" and "S3Config" in sc
        ]
    except ClientError:
        return []


def _list_matching_objects(s3, bucket, prefix, contact_id):
    keys, token = [], None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            resp = s3.list_objects_v2(**kwargs)
        except ClientError:
            break
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if contact_id in key:
                keys.append(key)
        token = resp.get("NextContinuationToken")
        if not token:
            break
    return keys


def _presign(s3, bucket, key, expires):
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )
    except ClientError:
        return None


def _classify_key(key):
    if "/Redacted/" in key or "/redacted/" in key or "_redacted." in key.lower():
        return "redacted"
    return "original"


def _search_prefixes(s3, bucket, prefixes, contact_id, expires):
    seen, results = set(), []
    for prefix in prefixes:
        for key in _list_matching_objects(s3, bucket, prefix, contact_id):
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "s3_uri":        f"s3://{bucket}/{key}",
                "presigned_url": _presign(s3, bucket, key, expires),
                "subtype":       _classify_key(key),
            })
    return results


def find_artifacts(connect, s3, instance_id, contact, expires):
    channel    = contact.get("Channel", "VOICE")
    ts         = contact.get("InitiationTimestamp")
    contact_id = contact["Id"]
    yyyy, mm, dd = ts.strftime("%Y"), ts.strftime("%m"), ts.strftime("%d")
    result: dict = {"recordings": [], "analysis": [], "transcripts": []}

    for cfg in _fetch_storage_configs(connect, instance_id, "CALL_RECORDINGS"):
        bucket = cfg["BucketName"]
        base   = cfg.get("BucketPrefix", "").rstrip("/")
        if channel == "VOICE":
            result["recordings"] += _search_prefixes(
                s3, bucket,
                [f"{base}/CallRecordings/{yyyy}/{mm}/{dd}/",
                 f"{base}/{yyyy}/{mm}/{dd}/"],
                contact_id, expires,
            )
            analysis_prefixes = [
                f"{base}/Analysis/Voice/{yyyy}/{mm}/{dd}/",
                f"{base}/Analysis/Voice/Redacted/{yyyy}/{mm}/{dd}/",
            ]
            if base:
                analysis_prefixes += [
                    f"Analysis/Voice/{yyyy}/{mm}/{dd}/",
                    f"Analysis/Voice/Redacted/{yyyy}/{mm}/{dd}/",
                ]
            result["analysis"] += _search_prefixes(s3, bucket, analysis_prefixes, contact_id, expires)
        elif channel == "CHAT":
            chat_prefixes = [
                f"{base}/Analysis/Chat/{yyyy}/{mm}/{dd}/",
                f"{base}/Analysis/Chat/Redacted/{yyyy}/{mm}/{dd}/",
            ]
            if base:
                chat_prefixes += [
                    f"Analysis/Chat/{yyyy}/{mm}/{dd}/",
                    f"Analysis/Chat/Redacted/{yyyy}/{mm}/{dd}/",
                ]
            result["analysis"] += _search_prefixes(s3, bucket, chat_prefixes, contact_id, expires)

    if channel == "CHAT":
        for cfg in _fetch_storage_configs(connect, instance_id, "CHAT_TRANSCRIPTS"):
            bucket = cfg["BucketName"]
            base   = cfg.get("BucketPrefix", "").rstrip("/")
            result["transcripts"] += _search_prefixes(
                s3, bucket,
                [f"{base}/{yyyy}/{mm}/{dd}/",
                 f"{base}/Redacted/{yyyy}/{mm}/{dd}/"],
                contact_id, expires,
            )
    return result


def print_recordings(contact, artifacts, expires):
    channel = contact.get("Channel", "?")
    ts      = contact.get("InitiationTimestamp")
    _section(f"RECORDINGS  {contact.get('Id', '?')}")
    _row("Channel:", channel)
    _row("Date:",    ts.strftime("%Y-%m-%d") if ts else "?")
    _row("URLs expire:", f"{expires}s ({expires // 60}m)")

    def _print_group(label, items):
        print(f"\n  {label}")
        if not items:
            print("    (none found)")
            return
        for subtype in ("original", "redacted"):
            group = [i for i in items if i["subtype"] == subtype]
            if not group:
                continue
            print(f"\n    [{subtype.upper()}]")
            for item in group:
                print(f"      S3:  {item['s3_uri']}")
                url = item["presigned_url"]
                print(f"      URL: {url if url else '(presign failed)'}")

    if channel == "VOICE":
        _print_group("RECORDINGS",           artifacts["recordings"])
        _print_group("CONTACT LENS ANALYSIS", artifacts["analysis"])
    elif channel == "CHAT":
        _print_group("CHAT TRANSCRIPTS",      artifacts["transcripts"])
        _print_group("CONTACT LENS ANALYSIS", artifacts["analysis"])
    else:
        for name, items in artifacts.items():
            _print_group(name.upper(), items)
    print()


def run_recordings(connect, s3, instance_id, contact, url_expires, output_json):
    artifacts = find_artifacts(connect, s3, instance_id, contact, url_expires)
    if not output_json:
        print_recordings(contact, artifacts, url_expires)
    ts = contact.get("InitiationTimestamp")
    return {
        "channel":             contact.get("Channel"),
        "date":                ts.strftime("%Y-%m-%d") if ts else None,
        "url_expires_seconds": url_expires,
        "artifacts":           artifacts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Raw logs download
# ══════════════════════════════════════════════════════════════════════════════

def run_logs(cw_events, contact_id, contact, log_group, output_json):
    start_ts = contact.get("InitiationTimestamp")
    end_ts   = contact.get("DisconnectTimestamp")

    if output_json:
        return {
            "log_group":   log_group,
            "event_count": len(cw_events),
            "window": {
                "start": start_ts.isoformat() if start_ts else None,
                "end":   end_ts.isoformat()   if end_ts   else None,
            },
            "events": [
                {
                    "timestamp":  dt.datetime.fromtimestamp(
                        ev["timestamp"] / 1000, tz=dt.timezone.utc
                    ).isoformat(),
                    "log_stream": ev.get("logStreamName"),
                    "message":    _parse_message(ev["message"]),
                }
                for ev in cw_events
            ],
        }

    # Non-JSON: write to file
    _section(f"RAW LOGS  {contact_id}")
    if not cw_events:
        print(f"  No flow log events found for this contact.")
        return {}

    print(f"  {len(cw_events)} event(s) found.")

    def _serial(o):
        return o.isoformat() if hasattr(o, "isoformat") else str(o)

    doc = {
        "contact_id": contact_id,
        "log_group":  log_group,
        "window": {
            "start": start_ts.isoformat() if start_ts else None,
            "end":   end_ts.isoformat()   if end_ts   else None,
        },
        "event_count": len(cw_events),
        "events": [
            {
                "timestamp":  dt.datetime.fromtimestamp(
                    ev["timestamp"] / 1000, tz=dt.timezone.utc
                ).isoformat(),
                "log_stream": ev.get("logStreamName"),
                "message":    _parse_message(ev["message"]),
            }
            for ev in cw_events
        ],
    }
    out_path = str(ct_snapshot.output_path("contact_investigator", f"{contact_id}_logs.json"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, default=_serial)
    print(f"  Saved → {out_path}")
    return {"event_count": len(cw_events), "saved_to": out_path}


# ── JSON serializer ───────────────────────────────────────────────────────────

def _serial(o):
    return o.isoformat() if hasattr(o, "isoformat") else str(o)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args = parse_args()

    need_logs = args.timeline or args.lambda_trace or args.logs
    need_s3   = args.recordings

    connect, logs_client, s3_client = make_clients(
        args.region, args.profile, need_logs=need_logs, need_s3=need_s3
    )

    # ── Shared: fetch contact ─────────────────────────────────────────────────
    contact  = fetch_contact(connect, args.instance_id, args.contact_id)
    start_ts = contact.get("InitiationTimestamp")
    end_ts   = contact.get("DisconnectTimestamp")

    if start_ts is None and need_logs:
        print("Error: contact has no InitiationTimestamp.", file=sys.stderr)
        sys.exit(1)

    # ── Shared: resolve names ─────────────────────────────────────────────────
    names = {}
    if args.overview or args.timeline:
        names = resolve_names(connect, args.instance_id, contact)

    # ── Shared: resolve log group ─────────────────────────────────────────────
    log_group = None
    if need_logs:
        log_group = resolve_log_group(connect, args.instance_id, args.log_group)
        print(f"  Log group : {log_group}", file=sys.stderr)

    # ── Shared: fetch CW flow log events (once) ───────────────────────────────
    cw_events: list = []
    if need_logs and log_group and start_ts:
        print("  Fetching flow logs...", file=sys.stderr)
        now      = dt.datetime.now(dt.timezone.utc)
        start_ms = _ms(start_ts - dt.timedelta(minutes=2))
        end_ms   = _ms(min(end_ts + dt.timedelta(minutes=5), now) if end_ts else now)
        cw_events = filter_log_events(
            logs_client, log_group,
            f'{{ $.ContactId = "{args.contact_id}" }}',
            start_ms, end_ms,
        )
        print(f"  Found {len(cw_events)} flow log event(s).", file=sys.stderr)
        if not cw_events and args.timeline:
            probe = filter_log_events(logs_client, log_group, "", start_ms, end_ms)
            if not probe:
                print(
                    f"  Warning: no events in {log_group} for this time window.\n"
                    "           Flow logging may not be enabled on this instance.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  Warning: flow logs exist for this window but contact {args.contact_id}\n"
                    "           was not found — the traversed flows may not have logging enabled.",
                    file=sys.stderr,
                )

    # ── Lens cache (shared by overview + timeline) ────────────────────────────
    lens_cache: dict = {}

    # ── Run sections ─────────────────────────────────────────────────────────
    results: dict = {}

    if args.overview:
        results["overview"] = run_overview(
            connect, args.instance_id, args.contact_id, contact,
            lens_cache, names, args.transcript, args.output_json,
        )

    if args.timeline:
        results["timeline"] = run_timeline(
            connect, logs_client, args.instance_id, args.contact_id, contact,
            log_group, cw_events, lens_cache, names, args.transcript, args.output_json,
        )

    if args.lambda_trace:
        print(f"  Lambda invocations: {len(extract_lambda_invocations(cw_events))}", file=sys.stderr)
        results["lambda"] = run_lambda(
            logs_client, args.contact_id, cw_events,
            args.lambda_logs, args.output_json,
        )

    if args.recordings:
        results["recordings"] = run_recordings(
            connect, s3_client, args.instance_id, contact,
            args.url_expires, args.output_json,
        )

    if args.logs:
        results["logs"] = run_logs(
            cw_events, args.contact_id, contact, log_group, args.output_json,
        )

    # ── JSON / file output ────────────────────────────────────────────────────
    if args.output_json or args.output:
        out = json.dumps(results, indent=2, default=_serial)
        if args.output:
            dest = ct_snapshot.output_path("contact_investigator", args.output)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"  Saved → {dest}", file=sys.stderr)
        else:
            print(out)


if __name__ == "__main__":
    main()
