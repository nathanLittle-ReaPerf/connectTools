#!/usr/bin/env python3
"""replay_contact.py — Replay a real contact as an HTML flow path visualization.

Pulls CloudWatch flow logs for a contact, builds a scenario from what actually
happened, and runs flow_sim.py to produce an HTML graph of the exact path taken.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_CFG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

SCRIPT_DIR    = Path(__file__).parent
SCENARIOS_DIR = SCRIPT_DIR / "Scenarios"
SIMS_DIR      = SCRIPT_DIR / "Simulations"

_MAN = """\
NAME
    replay_contact.py — Replay a real contact as an HTML flow path visualization

SYNOPSIS
    python replay_contact.py --instance-id UUID --contact-id UUID [OPTIONS]

DESCRIPTION
    Fetches CloudWatch flow logs for a real contact, builds a scenario from
    what actually happened (Lambda responses, DTMF inputs, attribute values),
    and runs flow_sim.py to produce an HTML graph of the exact path taken.

    Requires the flow cache (run flow_map.py first) and CloudWatch logs to
    still be within retention (usually 30 days).

    Scenario is written to flowSim/Scenarios/replay_<cid8>.json.
    HTML is written to flowSim/Simulations/replay_<cid8>.html.

OPTIONS
    --instance-id UUID   (required) Connect instance UUID
    --contact-id  UUID   (required) Contact ID to replay
    --region      REGION AWS region
    --profile     NAME   Named AWS profile for local use
    --html        FILE   Override HTML output path
    --no-html            Skip HTML; just write the scenario file
    --log-group   NAME   Override log group (default: auto-discovered)

IAM PERMISSIONS
    connect:DescribeContact
    connect:DescribeInstance
    logs:FilterLogEvents on /aws/connect/<instance-alias>
"""


# ── AWS helpers ────────────────────────────────────────────────────────────────

def _session(profile: str | None, region: str | None) -> boto3.Session:
    return boto3.Session(profile_name=profile or None, region_name=region or None)


def _epoch_ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def _describe_contact(connect, instance_id: str, contact_id: str) -> dict:
    return connect.describe_contact(InstanceId=instance_id, ContactId=contact_id)["Contact"]


def _resolve_log_group(connect, instance_id: str) -> str:
    resp = connect.describe_instance(InstanceId=instance_id)
    alias = resp["Instance"]["InstanceAlias"]
    return f"/aws/connect/{alias}"


# ── Log fetch ──────────────────────────────────────────────────────────────────

def _fetch_events(cw, log_group: str, contact_id: str, start: datetime, end: datetime) -> list[dict]:
    kwargs: dict = {
        "logGroupName": log_group,
        "startTime":    _epoch_ms(start),
        "endTime":      _epoch_ms(end),
        "filterPattern": f'{{ $.ContactId = "{contact_id}" }}',
        "limit": 10000,
    }
    events: list[dict] = []
    page = 0
    while True:
        page += 1
        print(f"  Fetching logs page {page} ...     ", end="\r", flush=True)
        try:
            resp = cw.filter_log_events(**kwargs)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                print(f"\n  Log group not found: {log_group}", file=sys.stderr)
                sys.exit(1)
            raise
        for ev in resp.get("events", []):
            try:
                msg = json.loads(ev["message"])
                if isinstance(msg, dict) and "ContactId" in msg:
                    events.append(msg)
            except (json.JSONDecodeError, KeyError):
                pass
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    print()
    return events


# ── Contact reconstruction ─────────────────────────────────────────────────────

def _val(obj: dict, *keys: str) -> str:
    for k in keys:
        if not isinstance(obj, dict):
            return ""
        obj = obj.get(k) or {}
    return obj if isinstance(obj, str) else ""


def _reconstruct(events: list[dict], target_id: str) -> dict | None:
    contacts: dict[str, dict] = {}
    events_sorted = sorted(events, key=lambda e: e.get("Timestamp") or e.get("EventTimestamp") or "")

    for ev in events_sorted:
        cid = ev.get("ContactId") or ev.get("InitialContactId") or ""
        if not cid:
            continue

        if cid not in contacts:
            contacts[cid] = {
                "contact_id":   cid,
                "initial_flow": "",
                "attributes":   defaultdict(list),
                "lambda_calls": [],
                "dtmf":         [],
                "hours":        [],
                "staffing":     [],
                "ani":          "",
                "channel":      "",
                "attr_sources": {},
            }
        c = contacts[cid]

        if not c["ani"]:
            c["ani"] = (_val(ev, "CustomerEndpoint", "Address")
                        or _val(ev, "ContactData", "CustomerEndpoint", "Address"))
        if not c["channel"]:
            c["channel"] = ev.get("Channel") or _val(ev, "ContactData", "Channel")

        flow_name  = ev.get("ContactFlowName") or ev.get("FlowName") or ""
        block_name = ev.get("BlockName") or ev.get("BlockLabel") or ev.get("Action") or ""

        if not c["initial_flow"] and flow_name:
            c["initial_flow"] = flow_name

        # SET attributes
        params = ev.get("Parameters") or {}
        if isinstance(params.get("Attributes"), dict):
            for k, v in params["Attributes"].items():
                if isinstance(v, str):
                    c["attributes"][k].append(v)
                    if k not in c["attr_sources"]:
                        c["attr_sources"][k] = "flow"

        # Lambda call
        arn = (params.get("LambdaFunctionARN") or params.get("FunctionArn") or "")
        results = ev.get("Results") or {}
        if not isinstance(results, dict):
            results = {}
        ext = {k: str(v) for k, v in results.items() if not k.startswith("_")}
        if arn or ext:
            fn = arn.split(":")[-1] if ":" in arn else (arn or "lambda")
            c["lambda_calls"].append({
                "arn":      arn,
                "result":   ev.get("ResultStatus") or ev.get("Status") or "Success",
                "external": ext,
            })
            for k in ext:
                c["attr_sources"][k] = fn  # Lambda source wins over flow

        # DTMF
        pressed = (results.get("Pressed") or results.get("DTMFInput")
                   or params.get("StoredCustomerInput") or "")
        if pressed:
            c["dtmf"].append({"flow": flow_name, "block": block_name, "pressed": str(pressed)})

        # Hours of operation
        hoo_id   = params.get("HoursOfOperationId") or params.get("HoursOfOperationArn") or ""
        in_hours = results.get("InHours") or results.get("CurrentStatus") or ""
        if hoo_id or in_hours:
            c["hours"].append({
                "flow": flow_name,
                "hoo_id":   hoo_id,
                "in_hours": str(in_hours).lower() in ("true", "in_hours", "1", "open"),
            })

        # Staffing
        queue_id = params.get("QueueId") or params.get("QueueArn") or ""
        staffed  = results.get("Staffed") or results.get("CurrentStatus") or ""
        if queue_id or staffed:
            c["staffing"].append({
                "flow":    flow_name,
                "queue_id": queue_id,
                "staffed":  str(staffed).lower() in ("true", "staffed", "1", "available"),
            })

    for c in contacts.values():
        c["attributes"] = dict(c["attributes"])

    # Return the exact contact if found; otherwise the first one (e.g. transfer chain)
    return contacts.get(target_id) or (next(iter(contacts.values())) if contacts else None)


# ── Scenario builder ───────────────────────────────────────────────────────────

def _build_scenario(contact: dict) -> dict:
    attr_sources = contact.get("attr_sources", {})
    scenario_attrs = {k: vals[-1] for k, vals in contact["attributes"].items() if vals}

    lambda_mocks: dict = {}
    for lc in contact["lambda_calls"]:
        fn = lc["arn"].split(":")[-1] if ":" in lc["arn"] else (lc["arn"] or "lambda")
        if fn not in lambda_mocks:
            lambda_mocks[fn] = {"result": lc["result"], "attributes": dict(lc.get("external") or {})}
        else:
            lambda_mocks[fn]["attributes"].update(lc.get("external") or {})

    dtmf_inputs: dict = {}
    for d in contact["dtmf"]:
        key = f"{d['flow']} / {d['block']}" if d.get("block") else d.get("flow", "")
        if key and key not in dtmf_inputs:
            dtmf_inputs[key] = {"value": d["pressed"]}

    hours_mocks: dict = {}
    for h in contact["hours"]:
        hid = h.get("hoo_id", "")
        if hid and hid not in hours_mocks:
            hours_mocks[hid] = {"in_hours": h["in_hours"]}

    staffing_mocks: dict = {}
    for s in contact["staffing"]:
        qid = s.get("queue_id", "")
        if qid and qid not in staffing_mocks:
            staffing_mocks[qid] = {"staffed": s["staffed"]}

    _attr_hints: dict = {}
    for k, v in scenario_attrs.items():
        src = attr_sources.get(k, "")
        _attr_hints[k] = f"[{src}] {v}" if src else v

    return {
        "_name":         f"Replay: {contact['contact_id'][:8]}",
        "_contact_id":   contact["contact_id"],
        "_initial_flow": contact.get("initial_flow", ""),
        "_contact_count": 1,
        "_note":         f"Replayed from real contact {contact['contact_id']}",
        "_attr_hints":   _attr_hints,
        "call_parameters": {
            "ani":     contact.get("ani", ""),
            "channel": contact.get("channel", "VOICE"),
        },
        "attributes":     scenario_attrs,
        "lambda_mocks":   lambda_mocks,
        "dtmf_inputs":    dtmf_inputs,
        "hours_mocks":    hours_mocks,
        "staffing_mocks": staffing_mocks,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="replay_contact.py",
        description="Replay a real contact as an HTML flow path visualization.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_MAN,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--contact-id",  required=True, metavar="UUID")
    p.add_argument("--region",      default=None)
    p.add_argument("--profile",     default=None)
    p.add_argument("--html",        default=None, metavar="FILE")
    p.add_argument("--no-html",     action="store_true")
    p.add_argument("--log-group",   default=None, metavar="NAME")
    return p


def main() -> None:
    if "--help-full" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args = parse_args().parse_args()
    sess    = _session(args.profile, args.region)
    connect = sess.client("connect", config=_CFG)
    cw      = sess.client("logs",    config=_CFG)

    print()
    print(f"  Instance : {args.instance_id}")
    print(f"  Contact  : {args.contact_id}")
    print()

    # 1 — Describe contact for time window
    print("  Fetching contact metadata ...", end=" ", flush=True)
    try:
        meta = _describe_contact(connect, args.instance_id, args.contact_id)
    except ClientError as e:
        print(f"\n  Error: {e}", file=sys.stderr)
        sys.exit(1)
    print("done")

    initiated    = meta.get("InitiationTimestamp")
    disconnected = meta.get("DisconnectTimestamp")
    now          = datetime.now(timezone.utc)

    if not initiated:
        print("  Error: contact has no InitiationTimestamp.", file=sys.stderr)
        sys.exit(1)

    start = initiated - timedelta(minutes=1)
    end   = (disconnected + timedelta(minutes=2)) if disconnected else now

    dur_str = ""
    if disconnected:
        dur = int((disconnected - initiated).total_seconds())
        dur_str = f"  ({dur}s)"
    print(f"  Started  : {initiated.strftime('%Y-%m-%d %H:%M:%S')} UTC{dur_str}")

    # 2 — Resolve log group
    log_group = args.log_group
    if not log_group:
        print("  Resolving log group ...", end=" ", flush=True)
        try:
            log_group = _resolve_log_group(connect, args.instance_id)
            print(log_group)
        except ClientError as e:
            print(f"\n  Error: {e}", file=sys.stderr)
            sys.exit(1)

    # 3 — Fetch log events
    print(f"  Querying {log_group} ...")
    try:
        raw_events = _fetch_events(cw, log_group, args.contact_id, start, end)
    except ClientError as e:
        print(f"\n  Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not raw_events:
        print()
        print("  No flow log events found for this contact.")
        print("  Possible causes:")
        print("    - Logs outside CloudWatch retention period (usually 30 days)")
        print("    - Contact was disconnected before entering a flow")
        print("    - Wrong log group — try --log-group /aws/connect/<alias>")
        sys.exit(1)

    print(f"  {len(raw_events)} log event(s) found.")

    # 4 — Reconstruct contact record
    contact = _reconstruct(raw_events, args.contact_id)
    if not contact:
        print("  Error: could not reconstruct contact from log events.", file=sys.stderr)
        sys.exit(1)

    if contact["contact_id"] != args.contact_id:
        print(f"  Note: entry contact in logs is {contact['contact_id'][:8]}… (transfer chain)")

    initial_flow = contact.get("initial_flow", "")
    if not initial_flow:
        print("  Warning: could not detect entry flow from logs.")
        try:
            initial_flow = input("  Enter starting flow name: ").strip()
        except (EOFError, KeyboardInterrupt):
            initial_flow = ""
        if not initial_flow:
            print("  Aborted.", file=sys.stderr)
            sys.exit(1)

    print(f"  Entry flow : {initial_flow}")
    steps = sum(1 for e in raw_events if e.get("ContactFlowName") or e.get("BlockName"))
    print(f"  Flow steps : {steps}")
    if contact["lambda_calls"]:
        print(f"  Lambdas    : {len(contact['lambda_calls'])}")
    if contact["dtmf"]:
        print(f"  DTMF       : {len(contact['dtmf'])}")

    # 5 — Build and write scenario
    scenario = _build_scenario(contact)
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    SIMS_DIR.mkdir(parents=True, exist_ok=True)

    cid8 = args.contact_id[:8]
    scenario_path = SCENARIOS_DIR / f"replay_{cid8}.json"
    scenario_path.write_text(json.dumps(scenario, indent=2), encoding="utf-8")
    print(f"\n  Scenario -> {scenario_path}")

    if args.no_html:
        print("  Skipping HTML (--no-html).")
        print()
        return

    # 6 — Run flow_sim.py
    html_path = args.html or str(SIMS_DIR / f"replay_{cid8}.html")
    flow_sim  = SCRIPT_DIR / "flow_sim.py"
    if not flow_sim.exists():
        print(f"  Error: flow_sim.py not found at {flow_sim}", file=sys.stderr)
        sys.exit(1)

    print(f"  Simulating  ...\n")

    cmd = [
        sys.executable, str(flow_sim),
        "--instance-id", args.instance_id,
        "--flow",        initial_flow,
        "--scenario",    str(scenario_path),
        "--html",        html_path,
    ]
    if args.region:
        cmd += ["--region", args.region]
    if args.profile:
        cmd += ["--profile", args.profile]

    try:
        result = subprocess.run(cmd)
    except Exception as e:
        print(f"  Error running flow_sim.py: {e}", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        sys.exit(result.returncode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Interrupted.")
        sys.exit(0)
