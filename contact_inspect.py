#!/usr/bin/env python3
"""contact-inspect: Pull all available data for an Amazon Connect contact."""

import argparse
import datetime as dt
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})
LENS_RETENTION_HOURS = 24

# Max 6 reference types per ListContactReferences call
REFERENCE_TYPES = ["URL", "ATTACHMENT", "CONTACT_ANALYSIS", "NUMBER", "STRING", "DATE"]


# ── Argument parsing ─────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Inspect an Amazon Connect contact: metadata, attributes, references, and Contact Lens.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --contact-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --contact-id <UUID> --transcript
  %(prog)s --instance-id <UUID> --contact-id <UUID> --json | jq '.contact.Channel'
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--contact-id", required=True, metavar="UUID")
    p.add_argument("--region", default=None, help="AWS region (defaults to session/CloudShell region)")
    p.add_argument("--profile", default=None, help="AWS named profile")
    p.add_argument("--json", action="store_true", dest="output_json", help="Emit raw JSON (pipe-friendly)")
    p.add_argument("--transcript", action="store_true", help="Print full Contact Lens transcript turns")
    return p.parse_args()


def make_client(region, profile):
    session = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


# ── Data fetchers ────────────────────────────────────────────────────────────

def fetch_contact(client, instance_id, contact_id):
    return client.describe_contact(InstanceId=instance_id, ContactId=contact_id)["Contact"]


def fetch_attributes(client, instance_id, contact_id):
    """Returns flat key-value dict of contact attributes, or {"_error": ...} on failure."""
    try:
        return client.get_contact_attributes(
            InstanceId=instance_id, InitialContactId=contact_id
        ).get("Attributes", {})
    except ClientError as e:
        return {"_error": str(e)}


def fetch_references(client, instance_id, contact_id):
    """Paginate ListContactReferences; returns list of reference summary dicts."""
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
            resp = client.list_contact_references(**kwargs)
        except ClientError as e:
            return [{"_error": str(e)}]
        refs.extend(resp.get("ReferenceSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return refs


def fetch_lens_voice(client, instance_id, contact_id):
    """
    ListRealtimeContactAnalysisSegments (voice).
    Returns (segments, error_string). Rate limit: 1 req/s burst 2.
    """
    segs, token = [], None
    while True:
        kwargs = dict(InstanceId=instance_id, ContactId=contact_id)
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_realtime_contact_analysis_segments(**kwargs)
        except ClientError as e:
            return None, str(e)
        segs.extend(resp.get("Segments", []))
        token = resp.get("NextToken")
        if not token:
            return segs, None


def fetch_lens_chat(client, instance_id, contact_id):
    """
    ListRealtimeContactAnalysisSegmentsV2 (chat/email).
    Returns (segments, status, error_string).
    """
    segs, token, status = [], None, None
    while True:
        kwargs = dict(
            InstanceId=instance_id,
            ContactId=contact_id,
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
        token = resp.get("NextToken")
        if not token:
            return segs, status, None


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


def fetch_transfer_chain(client, instance_id, contact):
    """
    Walk PreviousContactId backwards to reconstruct the full transfer chain.
    Returns list ordered oldest-first, NOT including the current contact.
    """
    chain, seen = [], {contact["Id"]}
    current = contact
    while True:
        prev_id = current.get("PreviousContactId")
        if not prev_id or prev_id in seen:
            break
        seen.add(prev_id)
        try:
            prev = client.describe_contact(InstanceId=instance_id, ContactId=prev_id)["Contact"]
            chain.insert(0, prev)
            current = prev
        except ClientError:
            break
    return chain


# ── Lens window check ────────────────────────────────────────────────────────

def lens_age_hours(contact):
    """Hours since contact ended, or None if still active."""
    end = contact.get("DisconnectTimestamp")
    if end is None:
        return None
    return (dt.datetime.now(tz=dt.timezone.utc) - end).total_seconds() / 3600


def collect_lens(client, instance_id, contact):
    """
    Returns a dict with one of these shapes:
      {"skipped": "reason"}          — outside window or unsupported channel
      {"error": "message"}           — API call failed
      {"segments": [...]}            — success (voice)
      {"segments": [...], "status":} — success (chat)
    """
    channel = contact.get("Channel", "")
    age_h = lens_age_hours(contact)

    if age_h is not None and age_h >= LENS_RETENTION_HOURS:
        return {
            "skipped": (
                f"Data expired ({int(age_h)}h old; {LENS_RETENTION_HOURS}h retention limit). "
                "Check your S3 export bucket if Contact Lens output is configured there."
            )
        }

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

    return {"skipped": f"Contact Lens not supported for channel: {channel}"}


# ── Formatting helpers ───────────────────────────────────────────────────────

_LABEL_W = 28


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
    end = contact.get("DisconnectTimestamp")
    return (end - start).total_seconds() if start and end else None


# ── Human-readable output ────────────────────────────────────────────────────

def _section(title):
    print(f"\n{'─' * 64}")
    print(f"  {title}")
    print(f"{'─' * 64}")


def _row(label, value):
    print(f"  {label:<{_LABEL_W}} {value}")


def print_human(contact, attributes, references, chain, lens_data, show_transcript, names=None):
    names = names or {}
    # ── Core ─────────────────────────────────────────────────────────────────
    _section(f"CONTACT  {contact.get('Id', '?')}")
    _row("Channel:", contact.get("Channel", "?"))
    _row("Initiation method:", contact.get("InitiationMethod", "?"))
    _row("Initiated:", fmt_ts(contact.get("InitiationTimestamp")))
    _row("Disconnected:", fmt_ts(contact.get("DisconnectTimestamp")))
    if contact.get("DisconnectReason"):
        _row("Disconnect reason:", contact["DisconnectReason"])
    _row("Duration:", fmt_dur(contact_secs(contact)))

    qi = contact.get("QueueInfo") or {}
    if qi.get("Id"):
        queue_label = names.get("queue") or qi["Id"]
        _row("Queue:", queue_label)
        if names.get("queue"):
            _row("Queue ID:", qi["Id"])
        _row("Enqueued:", fmt_ts(qi.get("EnqueueTimestamp")))

    ai = contact.get("AgentInfo") or {}
    if ai.get("Id"):
        agent_label = names.get("agent") or ai["Id"]
        _row("Agent:", agent_label)
        if names.get("agent"):
            _row("Agent ID:", ai["Id"])
        _row("Agent connected:", fmt_ts(ai.get("ConnectedToAgentTimestamp")))
        if ai.get("AgentPauseDurationInSeconds"):
            _row("Agent pause total:", fmt_dur(ai["AgentPauseDurationInSeconds"]))

    ce = contact.get("CustomerEndpoint") or {}
    if ce.get("Address"):
        _row("Customer endpoint:", f"{ce['Address']} ({ce.get('Type', '?')})")

    se = contact.get("SystemEndpoint") or {}
    if se.get("Address"):
        _row("System endpoint:", f"{se['Address']} ({se.get('Type', '?')})")

    # ── Transfer chain ───────────────────────────────────────────────────────
    if chain:
        _section("TRANSFER CHAIN  (oldest → current)")
        for c in chain:
            _row(
                f"  {c.get('Id', '?')[:8]}…",
                f"({c.get('Channel', '?')})  {fmt_ts(c.get('InitiationTimestamp'))}",
            )
        _row("  ↳ current:", contact["Id"])

    # ── Contact attributes ───────────────────────────────────────────────────
    if attributes:
        _section("CONTACT ATTRIBUTES")
        if "_error" in attributes:
            print(f"  Error retrieving attributes: {attributes['_error']}")
        else:
            for k, v in sorted(attributes.items()):
                _row(f"  {k}", v)

    # ── References ───────────────────────────────────────────────────────────
    if references:
        _section("REFERENCES")
        for ref in references:
            if "_error" in ref:
                print(f"  Error: {ref['_error']}")
                continue
            for rtype, detail in ref.items():
                if isinstance(detail, dict):
                    val = detail.get("Value") or detail.get("Url") or str(detail)
                else:
                    val = str(detail)
                _row(f"  [{rtype}]", val)

    # ── Contact Lens ─────────────────────────────────────────────────────────
    _section("CONTACT LENS")

    if "skipped" in lens_data:
        print(f"  {lens_data['skipped']}")
        return

    if "error" in lens_data:
        print(f"  Unavailable: {lens_data['error']}")
        return

    segs = lens_data.get("segments")
    if segs is None:
        print("  No data.")
        return

    if lens_data.get("status"):
        _row("Analysis status:", lens_data["status"])

    transcripts = [s["Transcript"] for s in segs if "Transcript" in s]
    categories  = [s["Categories"] for s in segs if "Categories" in s]
    summaries   = [s["PostContactSummary"] for s in segs if "PostContactSummary" in s]

    _row("Transcript turns:", str(len(transcripts)))

    # Per-role sentiment
    role_sentiments: dict = {}
    for t in transcripts:
        role = t.get("ParticipantRole", "UNKNOWN")
        sent = t.get("Sentiment")
        if sent:
            role_sentiments.setdefault(role, []).append(sent)

    SENT_SYM = {"POSITIVE": "+", "NEUTRAL": "=", "NEGATIVE": "-"}
    for role, sent_list in sorted(role_sentiments.items()):
        counts = {s: sent_list.count(s) for s in ["POSITIVE", "NEUTRAL", "NEGATIVE"]}
        dominant = max(counts, key=counts.get)
        detail = "  ".join(f"{SENT_SYM[k]}{counts[k]}" for k in ["POSITIVE", "NEUTRAL", "NEGATIVE"])
        _row(f"  {role} sentiment:", f"{dominant}  ({detail})")

    # Issues detected (embedded in transcript turn-level Issues lists)
    all_issues = []
    for t in transcripts:
        for issue in t.get("Issues", []):
            txt = issue.get("IssueDetected", {}).get("Text") or issue.get("Text", "")
            if txt:
                all_issues.append(f'"{txt}"')
    if all_issues:
        _row("Issues detected:", ", ".join(all_issues))

    # Categories
    for cat in categories:
        names = cat.get("MatchedCategories", [])
        if names:
            _row("Categories:", ", ".join(names))

    # Post-contact summary
    for pcs in summaries:
        content = pcs.get("Content", "")
        if content:
            print("\n  Post-contact summary:")
            for line in content.splitlines():
                print(f"    {line}")

    # Transcript turns
    if show_transcript and transcripts:
        print("\n  Transcript:")
        for t in transcripts:
            role = t.get("ParticipantRole", "?")
            content = t.get("Content", "")
            begin_ms = t.get("BeginOffsetMillis", 0)
            m, s = divmod(begin_ms // 1000, 60)
            print(f"    [{m:02d}:{s:02d}] {role}: {content}")
    elif transcripts and not show_transcript:
        print(f"\n  ({len(transcripts)} turns — use --transcript to print)")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    client = make_client(args.region, args.profile)

    # Fail fast: contact must exist before we make any other calls
    try:
        contact = fetch_contact(client, args.instance_id, args.contact_id)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"]["Message"]
        print(f"Error [{code}]: {msg}", file=sys.stderr)
        sys.exit(1)

    attributes = fetch_attributes(client, args.instance_id, args.contact_id)
    references = fetch_references(client, args.instance_id, args.contact_id)
    chain      = fetch_transfer_chain(client, args.instance_id, contact)
    lens_data  = collect_lens(client, args.instance_id, contact)

    # Resolve human-readable names for queue and agent
    names = {}
    qi = contact.get("QueueInfo") or {}
    if qi.get("Id"):
        names["queue"] = fetch_queue_name(client, args.instance_id, qi["Id"])
    ai = contact.get("AgentInfo") or {}
    if ai.get("Id"):
        names["agent"] = fetch_agent_name(client, args.instance_id, ai["Id"])

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)

        print(json.dumps(
            {
                "contact": contact,
                "attributes": attributes,
                "references": references,
                "transfer_chain": chain,
                "contact_lens": lens_data,
                "names": names,
            },
            indent=2,
            default=serial,
        ))
    else:
        print_human(contact, attributes, references, chain, lens_data, args.transcript, names)


if __name__ == "__main__":
    main()
