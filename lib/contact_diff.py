#!/usr/bin/env python3
"""contact_diff.py — Side-by-side comparison of two Amazon Connect contacts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import NamedTuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG          = Config(retries={"max_attempts": 5, "mode": "adaptive"})
LENS_RETENTION_HOURS  = 24
REFERENCE_TYPES       = ["URL", "ATTACHMENT", "CONTACT_ANALYSIS", "NUMBER", "STRING", "DATE"]

_MAN = """\
NAME
    contact_diff.py — Side-by-side comparison of two Amazon Connect contacts

SYNOPSIS
    python contact_diff.py --instance-id UUID --contact-id-a UUID --contact-id-b UUID [OPTIONS]

DESCRIPTION
    Compares two contacts field-by-field: core metadata (channel, queue, agent,
    duration, timestamps), custom contact attributes, and Contact Lens summaries
    (sentiment, categories, issues). Matching fields are dimmed; differing fields
    are highlighted. Use --all-attrs to see every attribute even when they match,
    or --json for machine-readable diff output.

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --contact-id-a UUID
        First contact UUID (contact A). Required.

    --contact-id-b UUID
        Second contact UUID (contact B). Required.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --all-attrs
        Show all contact attributes, not just those that differ between the two contacts.

    --json
        Emit a single JSON document with raw contact data and the full diff table.

EXAMPLES
    # Human-readable side-by-side diff
    python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID>

    # Show all attributes (not just differing ones)
    python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --all-attrs

    # Raw JSON output
    python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --json

IAM PERMISSIONS
    connect:DescribeContact
    connect:GetContactAttributes
    connect:ListRealtimeContactAnalysisSegments
    connect:DescribeQueue
    connect:DescribeUser

NOTES
    Contact Lens data has a 24-hour retention window. Expired contacts will show
    "Expired (>24h)" in the Contact Lens section of the diff.
"""

_LW = 22   # label column width
_VW = 24   # each value column width


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Compare two Amazon Connect contacts side by side.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --json
  %(prog)s --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --all-attrs
        """,
    )
    p.add_argument("--instance-id",   required=True, metavar="UUID")
    p.add_argument("--contact-id-a",  required=True, metavar="UUID")
    p.add_argument("--contact-id-b",  required=True, metavar="UUID")
    p.add_argument("--region",  default=None, help="AWS region (defaults to session/CloudShell region)")
    p.add_argument("--profile", default=None, help="AWS named profile")
    p.add_argument("--json",      action="store_true", dest="output_json",
                   help="Emit raw JSON (pipe-friendly)")
    p.add_argument("--all-attrs", action="store_true",
                   help="Show all attributes, not just differing ones")
    return p.parse_args()


# ── AWS client ────────────────────────────────────────────────────────────────

def make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


# ── Data fetchers (copied from contact_inspect.py) ────────────────────────────

def fetch_contact(client, instance_id, contact_id):
    return client.describe_contact(InstanceId=instance_id, ContactId=contact_id)["Contact"]


def fetch_attributes(client, instance_id, contact_id):
    try:
        return client.get_contact_attributes(
            InstanceId=instance_id, InitialContactId=contact_id
        ).get("Attributes", {})
    except ClientError as e:
        return {"_error": str(e)}


def fetch_queue_name(client, instance_id, queue_id):
    try:
        return client.describe_queue(InstanceId=instance_id, QueueId=queue_id)["Queue"]["Name"]
    except ClientError:
        return None


def fetch_agent_name(client, instance_id, agent_id):
    try:
        info = client.describe_user(InstanceId=instance_id, UserId=agent_id)["User"]["IdentityInfo"]
        return f"{info.get('FirstName', '')} {info.get('LastName', '')}".strip() or None
    except ClientError:
        return None


def fetch_lens_voice(client, instance_id, contact_id):
    segs, token = [], None
    while True:
        kwargs = dict(
            InstanceId=instance_id,
            ContactId=contact_id,
            OutputType="Raw",
            SegmentTypes=["TRANSCRIPT", "CATEGORIES", "ISSUES", "SENTIMENT"],
        )
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_realtime_contact_analysis_segments_v2(**kwargs)
        except ClientError as e:
            return None, str(e)
        segs.extend(resp.get("Segments", []))
        token = resp.get("NextToken")
        if not token:
            return segs, None


def fetch_lens_chat(client, instance_id, contact_id):
    segs, token, status = [], None, None
    while True:
        kwargs = dict(
            InstanceId=instance_id,
            ContactId=contact_id,
            OutputType="Raw",
            SegmentTypes=[
                "TRANSCRIPT", "CATEGORIES", "ISSUES",
                "EVENT", "ATTACHMENTS", "POST_CONTACT_SUMMARY",
            ],
        )
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_realtime_contact_analysis_segments_v2(**kwargs)
        except ClientError as e:
            return None, None, str(e)
        segs.extend(resp.get("Segments", []))
        status = resp.get("Status")
        token  = resp.get("NextToken")
        if not token:
            return segs, status, None


def lens_age_hours(contact):
    end = contact.get("DisconnectTimestamp")
    if end is None:
        return None
    return (dt.datetime.now(tz=dt.timezone.utc) - end).total_seconds() / 3600


def collect_lens(client, instance_id, contact):
    channel = contact.get("Channel", "")
    age_h   = lens_age_hours(contact)

    if age_h is not None and age_h >= LENS_RETENTION_HOURS:
        return {"skipped": "Expired (>24h)"}

    if channel == "VOICE":
        segs, err = fetch_lens_voice(client, instance_id, contact["Id"])
        return {"error": err} if err else {"segments": segs}

    if channel in ("CHAT", "EMAIL"):
        segs, status, err = fetch_lens_chat(client, instance_id, contact["Id"])
        if err:
            return {"error": err}
        result = {"segments": segs}
        if status:
            result["status"] = status
        return result

    return {"skipped": f"Not supported for channel: {channel}"}


# ── Formatting helpers (copied from contact_inspect.py) ───────────────────────

def fmt_ts(ts):
    if ts is None:
        return "—"
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(ts)


def fmt_dur(seconds):
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s" if m else f"{s}s"


def contact_secs(contact):
    start = contact.get("InitiationTimestamp")
    end   = contact.get("DisconnectTimestamp")
    return (end - start).total_seconds() if start and end else None


# ── Diff types ────────────────────────────────────────────────────────────────

class DiffRow(NamedTuple):
    label: str
    val_a: str
    val_b: str
    match: bool


def _mkrow(label: str, a, b) -> DiffRow:
    sa = str(a) if a is not None else "—"
    sb = str(b) if b is not None else "—"
    return DiffRow(label, sa, sb, sa == sb)


# ── Name resolution ───────────────────────────────────────────────────────────

def resolve_names(client, instance_id, contact) -> dict:
    names = {}
    qi = contact.get("QueueInfo") or {}
    if qi.get("Id"):
        names["queue"] = fetch_queue_name(client, instance_id, qi["Id"])
    ai = contact.get("AgentInfo") or {}
    if ai.get("Id"):
        names["agent"] = fetch_agent_name(client, instance_id, ai["Id"])
    return names


# ── Diff builders ─────────────────────────────────────────────────────────────

def build_core_rows(ca, cb, names_a, names_b) -> list:
    rows = []

    rows.append(_mkrow("Channel",          ca.get("Channel"),          cb.get("Channel")))
    rows.append(_mkrow("Initiation method", ca.get("InitiationMethod"), cb.get("InitiationMethod")))

    qi_a = ca.get("QueueInfo") or {}
    qi_b = cb.get("QueueInfo") or {}
    rows.append(_mkrow(
        "Queue",
        names_a.get("queue") or qi_a.get("Id") or "—",
        names_b.get("queue") or qi_b.get("Id") or "—",
    ))

    ai_a = ca.get("AgentInfo") or {}
    ai_b = cb.get("AgentInfo") or {}
    rows.append(_mkrow(
        "Agent",
        names_a.get("agent") or ai_a.get("Id") or "—",
        names_b.get("agent") or ai_b.get("Id") or "—",
    ))

    rows.append(_mkrow("Duration",        fmt_dur(contact_secs(ca)), fmt_dur(contact_secs(cb))))
    rows.append(_mkrow("Initiated",       fmt_ts(ca.get("InitiationTimestamp")), fmt_ts(cb.get("InitiationTimestamp"))))
    rows.append(_mkrow("Disconnected",    fmt_ts(ca.get("DisconnectTimestamp")), fmt_ts(cb.get("DisconnectTimestamp"))))
    rows.append(_mkrow("Disconnect reason", ca.get("DisconnectReason") or "—",   cb.get("DisconnectReason") or "—"))

    ce_a = (ca.get("CustomerEndpoint") or {}).get("Address") or "—"
    ce_b = (cb.get("CustomerEndpoint") or {}).get("Address") or "—"
    rows.append(_mkrow("Customer endpoint", ce_a, ce_b))

    rows.append(_mkrow("Previous contact",
                        ca.get("PreviousContactId") or "—",
                        cb.get("PreviousContactId") or "—"))

    return rows


def build_attr_rows(attrs_a: dict, attrs_b: dict) -> list:
    err_a = attrs_a.get("_error")
    err_b = attrs_b.get("_error")

    if err_a or err_b:
        va = f"[API error] {err_a}" if err_a else "(ok)"
        vb = f"[API error] {err_b}" if err_b else "(ok)"
        return [DiffRow("[error fetching attributes]", va, vb, va == vb)]

    all_keys = sorted(set(list(attrs_a.keys()) + list(attrs_b.keys())))
    rows = []
    for k in all_keys:
        va = attrs_a.get(k, "[absent]")
        vb = attrs_b.get(k, "[absent]")
        rows.append(_mkrow(k, va, vb))
    return rows


def _sentiment_str(transcripts: list, role: str) -> str:
    SENT_SYM = {"POSITIVE": "+", "NEUTRAL": "=", "NEGATIVE": "-"}
    sent_list = [t.get("Sentiment") for t in transcripts
                 if t.get("ParticipantRole") == role and t.get("Sentiment")]
    if not sent_list:
        return "—"
    counts  = {s: sent_list.count(s) for s in ["POSITIVE", "NEUTRAL", "NEGATIVE"]}
    dominant = max(counts, key=counts.get)
    detail   = " ".join(f"{SENT_SYM[k]}{counts[k]}" for k in ["POSITIVE", "NEUTRAL", "NEGATIVE"])
    return f"{dominant} ({detail})"


def extract_lens_summary(lens_data: dict) -> dict:
    if "skipped" in lens_data:
        skipped = lens_data["skipped"]
        return {k: skipped if k == "status" else "—"
                for k in ("status", "turns", "agent_sentiment", "customer_sentiment",
                           "categories", "issues", "post_contact_summary")}

    if "error" in lens_data:
        err = f"Error: {lens_data['error']}"
        return {k: err if k == "status" else "—"
                for k in ("status", "turns", "agent_sentiment", "customer_sentiment",
                           "categories", "issues", "post_contact_summary")}

    segs        = lens_data.get("segments") or []
    transcripts = [s["Transcript"]          for s in segs if "Transcript"       in s]
    categories  = [s["Categories"]          for s in segs if "Categories"        in s]
    summaries   = [s["PostContactSummary"]  for s in segs if "PostContactSummary" in s]

    issues = []
    for t in transcripts:
        for issue in t.get("Issues", []):
            txt = issue.get("IssueDetected", {}).get("Text") or issue.get("Text", "")
            if txt:
                issues.append(f'"{txt}"')

    cats = []
    for cat in categories:
        cats.extend(cat.get("MatchedCategories", []))

    pcs = summaries[0].get("Content", "") if summaries else ""
    if len(pcs) > _VW * 2:
        pcs = pcs[:_VW * 2 - 1] + "…"

    api_status = lens_data.get("status") or "OK"

    return {
        "status":              api_status,
        "turns":               str(len(transcripts)),
        "agent_sentiment":     _sentiment_str(transcripts, "AGENT"),
        "customer_sentiment":  _sentiment_str(transcripts, "CUSTOMER"),
        "categories":          ", ".join(sorted(cats)) if cats else "(none)",
        "issues":              ", ".join(issues) if issues else "(none)",
        "post_contact_summary": pcs if pcs else "(none)",
    }


def build_lens_rows(lens_a: dict, lens_b: dict) -> list:
    sa = extract_lens_summary(lens_a)
    sb = extract_lens_summary(lens_b)

    labels = {
        "status":              "Status",
        "turns":               "Turns",
        "agent_sentiment":     "Agent sentiment",
        "customer_sentiment":  "Customer sentiment",
        "categories":          "Categories",
        "issues":              "Issues",
        "post_contact_summary": "Post-contact summary",
    }

    rows = []
    for key, label in labels.items():
        va = sa.get(key, "—")
        vb = sb.get(key, "—")
        rows.append(_mkrow(label, va, vb))
    return rows


# ── Human-readable output ─────────────────────────────────────────────────────

def _trunc(s: str, width: int) -> str:
    return s if len(s) <= width else s[:width - 1] + "…"


def _print_row(row: DiffRow):
    sym    = "\033[32m✓\033[0m" if row.match else "\033[31m✗\033[0m"
    va     = _trunc(row.val_a, _VW)
    vb     = _trunc(row.val_b, _VW)
    label  = _trunc(row.label, _LW)

    if row.match:
        va_fmt = f"\033[90m{va:<{_VW}}\033[0m"
        vb_fmt = f"\033[90m{vb:<{_VW}}\033[0m"
    else:
        va_fmt = f"\033[90m{va:<{_VW}}\033[0m" if row.val_a == "[absent]" else f"{va:<{_VW}}"
        vb_fmt = f"\033[90m{vb:<{_VW}}\033[0m" if row.val_b == "[absent]" else f"{vb:<{_VW}}"

    print(f"  {label:<{_LW}}  {va_fmt}  {sym}  {vb_fmt}")


def _print_section_header(title: str):
    print(f"\n  {'─' * 74}")
    print(f"  {title}")
    print(f"  {'─' * 74}")


def print_human(id_a, id_b, core_rows, attr_rows, lens_rows, all_attrs: bool):
    all_rows = core_rows + attr_rows + lens_rows
    n_total  = len(all_rows)
    n_match  = sum(1 for r in all_rows if r.match)
    n_diff   = n_total - n_match

    # ── Header ────────────────────────────────────────────────────────────────
    print(f"\n  {'─' * 74}")
    print(f"  CONTACT DIFF")
    print(f"  {'─' * 74}")
    print(f"  {'A':<{_LW}}  {id_a}")
    print(f"  {'B':<{_LW}}  {id_b}")
    print(f"\n  {n_diff} field(s) differ  ({n_match}/{n_total} match)")

    # ── Core ──────────────────────────────────────────────────────────────────
    _print_section_header("CORE")
    for row in core_rows:
        _print_row(row)

    # ── Attributes ────────────────────────────────────────────────────────────
    if not attr_rows:
        print(f"\n  ATTRIBUTES  \033[90m(none)\033[0m")
    else:
        differing = [r for r in attr_rows if not r.match]
        if not all_attrs and not differing:
            print(f"\n  ATTRIBUTES  \033[90m(all {len(attr_rows)} match)\033[0m")
        else:
            _print_section_header("ATTRIBUTES")
            to_show = attr_rows if all_attrs else differing
            for row in to_show:
                _print_row(row)
            hidden = len(attr_rows) - len(to_show)
            if hidden:
                print(f"\n  \033[90m  ({hidden} matching attribute(s) hidden — use --all-attrs to show)\033[0m")

    # ── Contact Lens ──────────────────────────────────────────────────────────
    _print_section_header("CONTACT LENS")
    for row in lens_rows:
        _print_row(row)

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    args   = parse_args()
    client = make_client(args.region, args.profile)

    if args.contact_id_a == args.contact_id_b:
        print("Warning: both contact IDs are the same — all fields will match.", file=sys.stderr)

    # Fail fast — both contacts must exist before further API calls
    try:
        contact_a = fetch_contact(client, args.instance_id, args.contact_id_a)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        print(f"Error loading contact A [{code}]: {msg}", file=sys.stderr)
        sys.exit(1)

    try:
        contact_b = fetch_contact(client, args.instance_id, args.contact_id_b)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        print(f"Error loading contact B [{code}]: {msg}", file=sys.stderr)
        sys.exit(1)

    attrs_a = fetch_attributes(client, args.instance_id, args.contact_id_a)
    attrs_b = fetch_attributes(client, args.instance_id, args.contact_id_b)
    lens_a  = collect_lens(client, args.instance_id, contact_a)
    lens_b  = collect_lens(client, args.instance_id, contact_b)
    names_a = resolve_names(client, args.instance_id, contact_a)
    names_b = resolve_names(client, args.instance_id, contact_b)

    core_rows = build_core_rows(contact_a, contact_b, names_a, names_b)
    attr_rows = build_attr_rows(attrs_a, attrs_b)
    lens_rows = build_lens_rows(lens_a, lens_b)

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)

        def rows_to_list(rows):
            return [{"field": r.label, "a": r.val_a, "b": r.val_b, "match": r.match}
                    for r in rows]

        print(json.dumps(
            {
                "contact_a":    contact_a,
                "contact_b":    contact_b,
                "attributes_a": attrs_a,
                "attributes_b": attrs_b,
                "lens_a":       lens_a,
                "lens_b":       lens_b,
                "names_a":      names_a,
                "names_b":      names_b,
                "diff": {
                    "core":       rows_to_list(core_rows),
                    "attributes": rows_to_list(attr_rows),
                    "lens":       rows_to_list(lens_rows),
                },
            },
            indent=2,
            default=serial,
        ))
    else:
        print_human(
            args.contact_id_a, args.contact_id_b,
            core_rows, attr_rows, lens_rows,
            args.all_attrs,
        )


if __name__ == "__main__":
    main()
