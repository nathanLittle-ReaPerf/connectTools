#!/usr/bin/env python3
"""scenario_from_logs.py — Build flow_sim.py scenario files from CloudWatch Connect flow logs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_MAN = """\
NAME
    scenario_from_logs.py — Build scenario files for flow_sim.py from real CW flow logs

SYNOPSIS
    python scenario_from_logs.py LOG_FILE [LOG_FILE ...] [OPTIONS]

DESCRIPTION
    Parses exported Amazon Connect CloudWatch flow log files (one JSON log event
    per line) and extracts:
      - Contact attribute SET operations (what keys and what values)
      - Lambda invocation results and $.External.* values returned
      - DTMF / GetUserInput choices made by callers
      - Hours-of-operation and staffing check outcomes
      - The sequence of flows and blocks each contact traversed

    From this data, it produces one or more scenario JSON files ready for use
    with flow_sim.py, pre-filled with realistic attribute values and mock data
    derived from real contacts.

INPUT FORMAT
    Each line in a log file must be a CloudWatch log event in one of these forms:
      - Raw JSON event (the "message" field from CloudWatch)
      - CloudWatch Logs Insights export (a JSON object with a "message" field)
      - Log group export (a JSON object with a "events" array, each with "message")

    The tool auto-detects the format.

    Typical CloudWatch export via console or CLI:
      aws logs filter-log-events \\
          --log-group-name /aws/connect/<alias> \\
          --start-time <epoch-ms> \\
          --output json > logs.json

    Or download via CloudWatch Logs Insights export to S3.

OPTIONS
    LOG_FILE [...]
        One or more log files to parse. Accepts plain JSON lines, CW Insights
        export JSON, or CW filter-log-events JSON (with "events" array).

    --out-dir DIR
        Directory to write scenario files. Default: current directory.

    --merge
        Merge all contacts into a single scenario file (median/mode values used
        for each attribute). Default: one file per unique contact journey.

    --top N
        When not using --merge, write scenario files for the N most common
        contact journeys (by flow sequence). Default: 5.

    --contact-id UUID
        Extract a single contact by ID instead of discovering all contacts.

    --anonymize
        Replace attribute values with anonymized placeholders (preserves
        structure but removes PII). Useful for sharing scenarios.

    --list
        List discovered contacts with their journey summaries; don't write files.

    --json
        Print the parsed contact data as JSON instead of writing scenario files.

    --summary
        Print a summary of all attribute keys, value distributions, and Lambda
        results found across all contacts.

EXAMPLES
    # Parse a CW export and write top-5 scenario files
    python scenario_from_logs.py contacts.json

    # Single contact
    python scenario_from_logs.py contacts.json --contact-id <UUID>

    # Merge all contacts into one representative scenario
    python scenario_from_logs.py logs/*.json --merge

    # Just list what was found
    python scenario_from_logs.py contacts.json --list

    # Summary of all attribute values seen
    python scenario_from_logs.py contacts.json --summary

    # Anonymize PII before writing
    python scenario_from_logs.py contacts.json --anonymize
"""

SCENARIOS_DIR = Path(__file__).parent / "Scenarios"

# ── Log parsing ────────────────────────────────────────────────────────────────

def _extract_messages(path: str) -> list[str]:
    """Extract raw log message strings from a file, handling multiple formats."""
    raw = open(path, encoding="utf-8", errors="replace").read().strip()
    if not raw:
        return []

    # Try to parse as JSON
    try:
        obj = json.loads(raw)
        # filter-log-events response: {"events": [{"message": "...", ...}]}
        if isinstance(obj, dict) and "events" in obj:
            return [e["message"] for e in obj["events"] if "message" in e]
        # Array of CW events
        if isinstance(obj, list):
            messages = []
            for item in obj:
                if isinstance(item, dict) and "message" in item:
                    messages.append(item["message"])
                elif isinstance(item, dict) and "ContactId" in item:
                    messages.append(json.dumps(item))
                elif isinstance(item, str):
                    messages.append(item)
            return messages
        # Single event object
        if isinstance(obj, dict) and "ContactId" in obj:
            return [raw]
        if isinstance(obj, dict) and "message" in obj:
            return [obj["message"]]
    except json.JSONDecodeError:
        pass

    # Fall back to line-by-line (JSON Lines)
    messages = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "message" in obj:
                messages.append(obj["message"])
            elif isinstance(obj, dict) and "ContactId" in obj:
                messages.append(line)
        except json.JSONDecodeError:
            continue
    return messages


def _parse_message(msg) -> dict | None:
    """Parse a single log message (string or already-parsed dict) into a structured event dict."""
    if isinstance(msg, dict):
        return msg if "ContactId" in msg else None
    try:
        obj = json.loads(msg)
        if not isinstance(obj, dict) or "ContactId" not in obj:
            return None
        return obj
    except (json.JSONDecodeError, TypeError):
        return None


def load_all_events(paths: list[str]) -> list[dict]:
    """Load and parse all log events from the given file paths."""
    events = []
    for path in paths:
        if not os.path.exists(path):
            print(f"Warning: file not found: {path}", file=sys.stderr)
            continue
        messages = _extract_messages(path)
        for msg in messages:
            ev = _parse_message(msg)
            if ev:
                events.append(ev)
    return events


# ── Contact reconstruction ─────────────────────────────────────────────────────

def _val(obj: dict, *keys: str, default=None):
    """Safely traverse nested keys."""
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k, None)
        if obj is None:
            return default
    return obj


def _extract_set_attributes(event: dict) -> dict[str, str]:
    """Extract attribute key→value pairs from an UpdateContactAttributes block."""
    # Parameters.Attributes is a dict, or Parameters.ContactData.Attributes
    attrs = {}
    params = event.get("Parameters", {}) or {}

    # Direct attributes dict
    if isinstance(params.get("Attributes"), dict):
        for k, v in params["Attributes"].items():
            if isinstance(v, str):
                attrs[k] = v

    # Sometimes stored under ContactData
    cd_attrs = _val(params, "ContactData", "Attributes")
    if isinstance(cd_attrs, dict):
        for k, v in cd_attrs.items():
            if isinstance(v, str):
                attrs[k] = v

    return attrs


def _extract_lambda_data(event: dict) -> dict | None:
    """Extract Lambda invocation data (ARN, result, external attrs returned)."""
    params = event.get("Parameters", {}) or {}
    results = event.get("Results", {}) or {}

    arn = (params.get("LambdaFunctionARN") or params.get("FunctionArn")
           or params.get("LambdaArn") or "")

    # External attributes returned by Lambda live under Results or
    # Parameters.ExternalResults
    ext = {}
    if isinstance(results, dict):
        ext = {k: str(v) for k, v in results.items() if not k.startswith("_")}
    ext_params = params.get("ExternalResults") or params.get("ExternalContactData") or {}
    if isinstance(ext_params, dict):
        ext.update({k: str(v) for k, v in ext_params.items()})

    status = event.get("ExternalResults", {}) or {}
    if isinstance(status, dict) and not ext:
        ext = {k: str(v) for k, v in status.items()}

    result_status = event.get("ResultStatus") or event.get("Status") or "Success"

    if not arn and not ext:
        return None
    return {"arn": arn, "result": result_status, "external": ext}


def _extract_dtmf(event: dict) -> dict | None:
    """Extract DTMF / GetUserInput press from a log event."""
    params = event.get("Parameters", {}) or {}
    results = event.get("Results", {})
    if not isinstance(results, dict):
        results = {}

    pressed = (results.get("Pressed") or results.get("DTMFInput")
               or params.get("StoredCustomerInput") or "")
    if not pressed:
        return None

    prompt_text = (params.get("Text") or params.get("SpeechText")
                   or params.get("PromptId") or "")
    options = []
    for cond in (params.get("MenuOptions") or params.get("Conditions") or []):
        if isinstance(cond, dict):
            v = cond.get("Value") or cond.get("Condition") or ""
            if v:
                options.append(str(v))

    return {"pressed": str(pressed), "prompt": prompt_text, "options": options}


def _extract_hours_check(event: dict) -> dict | None:
    """Extract hours-of-operation check result."""
    params = event.get("Parameters", {}) or {}
    results = event.get("Results", {})
    if not isinstance(results, dict):
        results = {}

    hoo_id = params.get("HoursOfOperationId") or params.get("HoursOfOperationArn") or ""
    in_hours = results.get("InHours") or results.get("CurrentStatus") or ""
    if not hoo_id and not in_hours:
        return None

    return {
        "hoo_id": hoo_id,
        "in_hours": str(in_hours).lower() in ("true", "in_hours", "1", "open"),
    }


def _extract_staffing_check(event: dict) -> dict | None:
    """Extract check-staffing result."""
    params = event.get("Parameters", {}) or {}
    results = event.get("Results", {})
    if not isinstance(results, dict):
        results = {}

    queue_id = params.get("QueueId") or params.get("QueueArn") or ""
    channel = params.get("Channel") or ""
    staffed = results.get("Staffed") or results.get("CurrentStatus") or ""
    if not queue_id and not staffed:
        return None

    return {
        "queue_id": queue_id,
        "channel": channel,
        "staffed": str(staffed).lower() in ("true", "staffed", "1", "available"),
    }


def _block_type_label(event: dict) -> str:
    etype = event.get("EventType") or event.get("Type") or ""
    action = event.get("Action") or event.get("BlockType") or ""
    return action or etype


def reconstruct_contacts(events: list[dict]) -> dict[str, dict]:
    """
    Group events by ContactId and build a per-contact record containing:
      - journey: ordered list of (flow_name, block_label, block_type)
      - attributes: {key: [values seen in order]}
      - lambda_calls: list of lambda data dicts
      - dtmf: list of dtmf press dicts
      - hours: list of hours check dicts
      - staffing: list of staffing check dicts
      - ani, dnis: caller/dialed numbers if present
    """
    contacts: dict[str, dict] = {}

    # Sort events by Timestamp for correct ordering
    def ts_key(ev: dict) -> str:
        return ev.get("Timestamp") or ev.get("EventTimestamp") or ""

    events_sorted = sorted(events, key=ts_key)

    for ev in events_sorted:
        cid = ev.get("ContactId") or ev.get("InitialContactId") or ""
        if not cid:
            continue

        if cid not in contacts:
            contacts[cid] = {
                "contact_id": cid,
                "journey": [],  # list of {"flow": str, "block": str, "type": str}
                "attributes": defaultdict(list),
                "lambda_calls": [],
                "dtmf": [],
                "hours": [],
                "staffing": [],
                "ani": "",
                "dnis": "",
                "channel": "",
                "initial_flow": "",
            }

        c = contacts[cid]

        # ANI / DNIS
        if not c["ani"]:
            ani = (_val(ev, "CustomerEndpoint", "Address")
                   or _val(ev, "ContactData", "CustomerEndpoint", "Address") or "")
            if ani:
                c["ani"] = ani
        if not c["dnis"]:
            dnis = (_val(ev, "SystemEndpoint", "Address")
                    or _val(ev, "ContactData", "SystemEndpoint", "Address") or "")
            if dnis:
                c["dnis"] = dnis
        if not c["channel"]:
            ch = (ev.get("Channel")
                  or _val(ev, "ContactData", "Channel") or "")
            if ch:
                c["channel"] = ch

        flow_name = ev.get("ContactFlowName") or ev.get("FlowName") or ""
        block_name = ev.get("BlockName") or ev.get("BlockLabel") or ev.get("Action") or ""
        block_type = _block_type_label(ev)

        if not c["initial_flow"] and flow_name:
            c["initial_flow"] = flow_name

        if flow_name or block_name:
            # Avoid duplicating back-to-back identical steps
            entry = {"flow": flow_name, "block": block_name, "type": block_type}
            if not c["journey"] or c["journey"][-1] != entry:
                c["journey"].append(entry)

        # SET attributes
        set_attrs = _extract_set_attributes(ev)
        for k, v in set_attrs.items():
            c["attributes"][k].append(v)

        # Lambda
        lambda_data = _extract_lambda_data(ev)
        if lambda_data and lambda_data.get("arn"):
            c["lambda_calls"].append(lambda_data)

        # DTMF
        dtmf = _extract_dtmf(ev)
        if dtmf and dtmf["pressed"]:
            c["dtmf"].append({"flow": flow_name, "block": block_name, **dtmf})

        # Hours
        hours = _extract_hours_check(ev)
        if hours:
            c["hours"].append({"flow": flow_name, "block": block_name, **hours})

        # Staffing
        staffing = _extract_staffing_check(ev)
        if staffing:
            c["staffing"].append({"flow": flow_name, "block": block_name, **staffing})

    # Convert defaultdicts
    for c in contacts.values():
        c["attributes"] = dict(c["attributes"])

    return contacts


# ── Analysis helpers ───────────────────────────────────────────────────────────

def _most_common(values: list) -> Any:
    """Return the most common value in a list."""
    if not values:
        return ""
    counts: dict = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda k: counts[k])


def _journey_key(contact: dict) -> str:
    """A string key representing the flow sequence (for grouping similar journeys)."""
    flows_seen = []
    for step in contact["journey"]:
        f = step.get("flow", "")
        if f and (not flows_seen or flows_seen[-1] != f):
            flows_seen.append(f)
    return " → ".join(flows_seen)


def _anonymize_value(key: str, value: str) -> str:
    """Replace PII-looking values with safe placeholders."""
    if not value:
        return value
    # Phone numbers
    if re.search(r"\+?[\d\s\-().]{7,}", value):
        return "+10000000000" if value.startswith("+1") else "PHONE_REDACTED"
    # Email
    if "@" in value and "." in value:
        return "user@example.com"
    # UUID
    if re.match(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value, re.I):
        return "00000000-0000-0000-0000-000000000000"
    # Looks like an account/member number (all digits, 6-20 chars)
    if re.match(r"\d{6,20}$", value):
        return "1234567890"
    return value


# ── Scenario file generation ───────────────────────────────────────────────────

def build_scenario_from_contact(contact: dict, anonymize: bool = False) -> dict:
    """Build a flow_sim.py scenario dict from a single reconstructed contact."""
    attrs = contact["attributes"]
    lambda_calls = contact["lambda_calls"]
    dtmf_list = contact["dtmf"]
    hours_list = contact["hours"]
    staffing_list = contact["staffing"]

    # ── Attributes ──
    scenario_attrs: dict[str, str] = {}
    attr_hints: dict[str, str] = {}
    for key, values in attrs.items():
        best = _most_common(values)
        if anonymize:
            best = _anonymize_value(key, best)
        scenario_attrs[key] = best
        unique_vals = sorted(set(values))
        if len(unique_vals) > 1:
            attr_hints[key] = f"values seen in this log: {', '.join(unique_vals[:10])}"
        elif unique_vals:
            attr_hints[key] = f"value seen: {unique_vals[0]}"

    # ── Lambda mocks ──
    lambda_mocks: dict[str, dict] = {}
    for call in lambda_calls:
        arn = call.get("arn", "")
        name = arn.split(":")[-1] if arn else "unknown"
        if name not in lambda_mocks:
            lambda_mocks[name] = {
                "_arn": arn,
                "result": call.get("result", "Success"),
                "attributes": {},
            }
        ext = call.get("external", {})
        if ext:
            lambda_mocks[name]["attributes"].update(
                {k: (_anonymize_value(k, v) if anonymize else v) for k, v in ext.items()}
            )

    # ── DTMF inputs ──
    dtmf_inputs: dict[str, dict] = {}
    for d in dtmf_list:
        block_key = f"{d.get('flow', '')} / {d.get('block', '')}"
        if block_key not in dtmf_inputs:
            dtmf_inputs[block_key] = {
                "options": d.get("options", []),
                "value": d["pressed"],
                "_prompt": d.get("prompt", ""),
            }

    # ── Hours mocks ──
    hours_mocks: dict[str, dict] = {}
    for h in hours_list:
        hid = h.get("hoo_id", "") or f"block_{h.get('block','')}"
        if hid not in hours_mocks:
            hours_mocks[hid] = {
                "in_hours": h.get("in_hours", True),
                "_block": h.get("block", ""),
                "_flow": h.get("flow", ""),
            }

    # ── Staffing mocks ──
    staffing_mocks: dict[str, dict] = {}
    for s in staffing_list:
        qid = s.get("queue_id", "") or f"block_{s.get('block','')}"
        if qid not in staffing_mocks:
            staffing_mocks[qid] = {
                "staffed": s.get("staffed", True),
                "channel": s.get("channel", ""),
                "_block": s.get("block", ""),
                "_flow": s.get("flow", ""),
            }

    # ── Journey note ──
    journey_key = _journey_key(contact)

    ani = contact.get("ani", "")
    dnis = contact.get("dnis", "")
    if anonymize:
        ani = _anonymize_value("ani", ani)
        dnis = _anonymize_value("dnis", dnis)

    return {
        "_note": (
            f"Generated by scenario_from_logs.py from contact {contact['contact_id']}. "
            f"Journey: {journey_key}"
        ),
        "_contact_id": contact["contact_id"] if not anonymize else "REDACTED",
        "_attr_hints": attr_hints,
        "call_parameters": {
            "ani": ani or None,
            "dnis": dnis or None,
            "channel": contact.get("channel", "VOICE") or "VOICE",
            "simulated_time": None,
        },
        "attributes": scenario_attrs,
        "lambda_mocks": lambda_mocks,
        "dtmf_inputs": dtmf_inputs,
        "hours_mocks": hours_mocks,
        "staffing_mocks": staffing_mocks,
    }


def build_merged_scenario(contacts: dict[str, dict], anonymize: bool = False) -> dict:
    """Merge all contacts into a single representative scenario."""
    all_attr_values: dict[str, list[str]] = defaultdict(list)
    all_lambda: dict[str, dict] = {}
    all_dtmf: dict[str, dict] = {}
    all_hours: dict[str, dict] = {}
    all_staffing: dict[str, dict] = {}
    attr_hints: dict[str, str] = {}

    for c in contacts.values():
        for k, vals in c["attributes"].items():
            all_attr_values[k].extend(vals)
        for call in c["lambda_calls"]:
            arn = call.get("arn", "")
            name = arn.split(":")[-1] if arn else "unknown"
            if name not in all_lambda:
                all_lambda[name] = {"_arn": arn, "result": call.get("result", "Success"), "attributes": {}}
            ext = call.get("external", {})
            if ext:
                all_lambda[name]["attributes"].update(ext)
        for d in c["dtmf"]:
            key = f"{d.get('flow','')} / {d.get('block','')}"
            if key not in all_dtmf:
                all_dtmf[key] = {"options": d.get("options", []), "value": d["pressed"], "_prompt": d.get("prompt","")}
        for h in c["hours"]:
            hid = h.get("hoo_id","") or f"block_{h.get('block','')}"
            if hid not in all_hours:
                all_hours[hid] = {"in_hours": h.get("in_hours", True), "_block": h.get("block",""), "_flow": h.get("flow","")}
        for s in c["staffing"]:
            qid = s.get("queue_id","") or f"block_{s.get('block','')}"
            if qid not in all_staffing:
                all_staffing[qid] = {"staffed": s.get("staffed", True), "channel": s.get("channel",""), "_block": s.get("block",""), "_flow": s.get("flow","")}

    scenario_attrs: dict[str, str] = {}
    for k, vals in all_attr_values.items():
        best = _most_common(vals)
        if anonymize:
            best = _anonymize_value(k, best)
        scenario_attrs[k] = best
        unique_vals = sorted(set(vals))
        if len(unique_vals) > 1:
            attr_hints[k] = f"values seen across {len(vals)} contacts: {', '.join(unique_vals[:10])}"
        elif unique_vals:
            attr_hints[k] = f"only value seen: {unique_vals[0]}"

    if anonymize:
        for name, lm in all_lambda.items():
            lm["attributes"] = {k: _anonymize_value(k, v) for k, v in lm["attributes"].items()}

    n = len(contacts)
    journey_counts: dict[str, int] = defaultdict(int)
    for c in contacts.values():
        journey_counts[_journey_key(c)] += 1
    top_journeys = sorted(journey_counts.items(), key=lambda x: -x[1])[:5]
    journey_note = "; ".join(f"{k} ({v}x)" for k, v in top_journeys)

    return {
        "_note": f"Generated by scenario_from_logs.py. Merged from {n} contacts. Top journeys: {journey_note}",
        "_contact_count": n,
        "_attr_hints": attr_hints,
        "call_parameters": {
            "ani": None,
            "dnis": None,
            "channel": "VOICE",
            "simulated_time": None,
        },
        "attributes": scenario_attrs,
        "lambda_mocks": all_lambda,
        "dtmf_inputs": all_dtmf,
        "hours_mocks": all_hours,
        "staffing_mocks": all_staffing,
    }


# ── Output helpers ─────────────────────────────────────────────────────────────

def _safe_filename(text: str) -> str:
    return re.sub(r"[^\w\-.]", "_", text)[:80]


def print_summary(contacts: dict[str, dict]) -> None:
    """Print a summary of all attribute keys and value distributions."""
    all_attr_values: dict[str, list[str]] = defaultdict(list)
    for c in contacts.values():
        for k, vals in c["attributes"].items():
            all_attr_values[k].extend(vals)

    print(f"\nContacts parsed: {len(contacts)}")
    print(f"Attribute keys found: {len(all_attr_values)}")
    print()

    if all_attr_values:
        max_key = max(len(k) for k in all_attr_values)
        print(f"{'Attribute':<{max_key}}  {'Count':>5}  Top values")
        print(f"{'-'*max_key}  {'-'*5}  {'-'*40}")
        for k in sorted(all_attr_values):
            vals = all_attr_values[k]
            counts: dict = {}
            for v in vals:
                counts[v] = counts.get(v, 0) + 1
            top = sorted(counts.items(), key=lambda x: -x[1])[:3]
            top_str = ", ".join(f"{v!r}({n})" for v, n in top)
            print(f"{k:<{max_key}}  {len(vals):>5}  {top_str}")

    print()
    lambda_arns: set[str] = set()
    dtmf_choices: dict[str, list] = defaultdict(list)
    for c in contacts.values():
        for call in c["lambda_calls"]:
            if call.get("arn"):
                lambda_arns.add(call["arn"])
        for d in c["dtmf"]:
            key = f"{d.get('flow','')} / {d.get('block','')}"
            dtmf_choices[key].append(d["pressed"])

    if lambda_arns:
        print("Lambda functions invoked:")
        for arn in sorted(lambda_arns):
            print(f"  {arn}")
        print()

    if dtmf_choices:
        print("DTMF inputs:")
        for key, presses in sorted(dtmf_choices.items()):
            counts: dict = {}
            for p in presses:
                counts[p] = counts.get(p, 0) + 1
            dist = ", ".join(f"{p}({n})" for p, n in sorted(counts.items(), key=lambda x: -x[1]))
            print(f"  {key}: {dist}")


def print_list(contacts: dict[str, dict]) -> None:
    """List contacts with journey summaries."""
    if not contacts:
        print("No contacts found.")
        return
    print(f"{'Contact ID':<36}  {'Channel':<7}  {'Attrs':>5}  Journey")
    print(f"{'-'*36}  {'-'*7}  {'-'*5}  {'-'*60}")
    for cid, c in sorted(contacts.items(), key=lambda x: x[0]):
        jk = _journey_key(c)
        ch = c.get("channel", "")[:7]
        na = len(c["attributes"])
        print(f"{cid:<36}  {ch:<7}  {na:>5}  {jk[:80]}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    p = argparse.ArgumentParser(
        description="Build flow_sim.py scenario files from CloudWatch Connect flow logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s contacts.json
  %(prog)s contacts.json --contact-id <UUID>
  %(prog)s logs/*.json --merge
  %(prog)s contacts.json --list
  %(prog)s contacts.json --summary
  %(prog)s contacts.json --anonymize
        """,
    )
    p.add_argument("log_files", nargs="+", metavar="LOG_FILE", help="CloudWatch export file(s)")
    p.add_argument("--out-dir",     default=str(SCENARIOS_DIR), metavar="DIR",
                   help="Output directory (default: flowSim/Scenarios/)")
    p.add_argument("--merge",       action="store_true",           help="Merge all contacts into one scenario")
    p.add_argument("--top",         default=5,     type=int,       help="Write top N journeys (default: 5)")
    p.add_argument("--contact-id",  default=None,  metavar="UUID", help="Extract a single contact by ID")
    p.add_argument("--anonymize",   action="store_true",           help="Redact PII from attribute values")
    p.add_argument("--list",        action="store_true",           help="List contacts; don't write files")
    p.add_argument("--summary",     action="store_true",           help="Print attribute/lambda summary")
    p.add_argument("--json",        action="store_true",           help="Print parsed data as JSON")
    args = p.parse_args()

    print(f"Loading {len(args.log_files)} file(s)...", file=sys.stderr)
    events = load_all_events(args.log_files)
    print(f"  {len(events)} log events loaded.", file=sys.stderr)

    if not events:
        print("No parseable log events found. Check file format (see --man).", file=sys.stderr)
        sys.exit(1)

    contacts = reconstruct_contacts(events)
    print(f"  {len(contacts)} unique contacts reconstructed.", file=sys.stderr)

    if not contacts:
        print("No contacts found in log events.", file=sys.stderr)
        sys.exit(1)

    # Filter to single contact if requested
    if args.contact_id:
        if args.contact_id not in contacts:
            # Try case-insensitive prefix match
            matches = [c for c in contacts if c.lower().startswith(args.contact_id.lower())]
            if not matches:
                print(f"Contact {args.contact_id!r} not found.", file=sys.stderr)
                sys.exit(1)
            if len(matches) > 1:
                print(f"Ambiguous: {len(matches)} contacts match prefix {args.contact_id!r}", file=sys.stderr)
                for m in matches:
                    print(f"  {m}", file=sys.stderr)
                sys.exit(1)
            args.contact_id = matches[0]
        contacts = {args.contact_id: contacts[args.contact_id]}

    # ── List mode ──
    if args.list:
        print_list(contacts)
        return

    # ── Summary mode ──
    if args.summary:
        print_summary(contacts)
        return

    # ── JSON mode ──
    if args.json:
        def serial(o):
            if isinstance(o, set):
                return list(o)
            return str(o)
        print(json.dumps({"contacts": list(contacts.values())}, default=serial, indent=2))
        return

    # ── Write scenario files ──
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []

    if args.merge or args.contact_id:
        scenario = (
            build_scenario_from_contact(contacts[args.contact_id], args.anonymize)
            if args.contact_id
            else build_merged_scenario(contacts, args.anonymize)
        )
        slug = (
            f"contact_{args.contact_id[:8]}"
            if args.contact_id
            else f"merged_{len(contacts)}_contacts"
        )
        path = out_dir / f"scenario_{slug}.json"
        path.write_text(json.dumps(scenario, indent=2), encoding="utf-8")
        written.append(str(path))
    else:
        # Group by journey key and write top-N
        journey_groups: dict[str, list[dict]] = defaultdict(list)
        for c in contacts.values():
            journey_groups[_journey_key(c)].append(c)

        top_groups = sorted(journey_groups.items(), key=lambda x: -len(x[1]))[: args.top]
        for rank, (journey_key, group) in enumerate(top_groups, 1):
            # Use the most recent contact as the representative
            rep = sorted(group, key=lambda c: c["contact_id"])[-1]
            scenario = build_scenario_from_contact(rep, args.anonymize)
            scenario["_note"] = (
                f"Generated by scenario_from_logs.py. "
                f"Journey #{rank} ({len(group)} contacts): {journey_key}"
            )
            flow_part = _safe_filename(journey_key.split(" → ")[0])[:40]
            path = out_dir / f"scenario_{rank:02d}_{flow_part}.json"
            path.write_text(json.dumps(scenario, indent=2), encoding="utf-8")
            written.append(str(path))

    print(f"\nWrote {len(written)} scenario file(s):")
    for p_str in written:
        print(f"  {p_str}")
    print("\nRun flow_sim.py with one of these files:")
    if written:
        first = Path(written[0]).name
        print(f"  python flow_sim.py --instance-id <UUID> --flow \"Main IVR\" --scenario {first}")


if __name__ == "__main__":
    main()
