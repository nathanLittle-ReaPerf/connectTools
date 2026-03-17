#!/usr/bin/env python3
"""orphaned_resources.py — Find unused resources in an Amazon Connect instance.

Scans all flow content to extract every resource reference (queues, prompts,
hours of operation, Lambda ARNs, and inter-flow calls), then cross-references
against the full instance inventory. Reports:

  - Flows not called by any other flow AND not assigned to any phone number
  - Queues / prompts / hours of operation in the instance but unreferenced
  - Lambda ARNs referenced in flows (for auditing; use --check-lambdas to
    probe each one against the Lambda API)
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import ct_snapshot

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

# ARN pattern — matches Connect and Lambda ARNs embedded in flow JSON strings
_ARN_RE = re.compile(r'arn:aws[a-z0-9-]*:[a-z0-9]+:[a-z0-9-]+:[0-9]+:[^\s,"\'{}[\]\\]+')

_MAN = """\
NAME
    orphaned_resources.py — Find unused resources in an Amazon Connect instance

SYNOPSIS
    python orphaned_resources.py --instance-id UUID [OPTIONS]

DESCRIPTION
    Fetches all contact flow content and extracts every resource reference
    (queues, prompts, hours of operation, Lambda ARNs, inter-flow calls).
    Cross-references against the full instance inventory to identify:

      Orphaned flows         — not called by any other flow and not assigned
                               to any phone number (entry point audit)
      Orphaned queues        — exist in the instance but not referenced in
                               any flow
      Orphaned prompts       — exist in the instance but not referenced in
                               any flow
      Orphaned hours         — hours-of-operation configs not referenced in
                               any flow
      Lambda references      — all unique Lambda ARNs used across flows
                               (use --check-lambdas to probe each one)

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --region REGION
        AWS region (e.g. us-east-1). Defaults to the session or CloudShell region.

    --profile NAME
        AWS named profile for local development.

    --check-lambdas
        For each Lambda ARN referenced in flows, call GetFunction to verify
        it still exists. Flags broken references.
        Requires lambda:GetFunction on each referenced function.

    --json
        Print results as JSON to stdout.

    --csv FILE
        Write orphaned-resource rows to ~/.connecttools/orphaned_resources/<FILE>.

EXAMPLES
    # Full orphan audit
    python orphaned_resources.py --instance-id <UUID> --region us-east-1

    # Also verify Lambda ARNs exist
    python orphaned_resources.py --instance-id <UUID> --check-lambdas

    # JSON output
    python orphaned_resources.py --instance-id <UUID> --json | jq '.orphaned_flows'

    # Export to CSV
    python orphaned_resources.py --instance-id <UUID> --csv orphans.csv

IAM PERMISSIONS
    connect:ListContactFlows
    connect:DescribeContactFlow
    connect:ListQueues
    connect:ListPrompts
    connect:ListHoursOfOperations
    connect:ListPhoneNumbersV2
    lambda:GetFunction  (only with --check-lambdas)

NOTES
    Flow content is fetched live (not from snapshot) since the snapshot does
    not store flow block definitions. All other resource lists use the snapshot
    when available to avoid extra API calls.
    Archived/non-published flows are included in the orphan check — a flow
    that is SAVED but never called by anything is still an orphan candidate.
"""


# ── Argument parsing ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Find unused resources in an Amazon Connect instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --check-lambdas
  %(prog)s --instance-id <UUID> --json | jq '.orphaned_flows'
  %(prog)s --instance-id <UUID> --csv orphans.csv
        """,
    )
    p.add_argument("--instance-id",    required=True, metavar="UUID")
    p.add_argument("--region",         default=None)
    p.add_argument("--profile",        default=None)
    p.add_argument("--check-lambdas",  action="store_true",
                   help="Probe each referenced Lambda ARN via GetFunction")
    p.add_argument("--json",  action="store_true", dest="output_json")
    p.add_argument("--csv",   default=None, metavar="FILE")
    return p.parse_args()


# ── Client factory ───────────────────────────────────────────────────────────────

def make_clients(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    connect = session.client("connect", region_name=resolved, config=RETRY_CONFIG)
    lam     = session.client("lambda",  region_name=resolved, config=RETRY_CONFIG)
    return connect, lam


# ── Paginators ───────────────────────────────────────────────────────────────────

def _paginate(client, method, list_key, **kwargs) -> list:
    items, token = [], None
    while True:
        if token:
            kwargs["NextToken"] = token
        try:
            resp = getattr(client, method)(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDeniedException", "ResourceNotFoundException",
                        "UnsupportedOperationException"):
                return []
            raise
        items.extend(resp.get(list_key, []))
        token = resp.get("NextToken")
        if not token:
            return items


def fetch_all_flows(client, instance_id) -> list:
    return _paginate(client, "list_contact_flows", "ContactFlowSummaryList",
                     InstanceId=instance_id, MaxResults=100)


def fetch_all_queues(client, instance_id, snapshot) -> dict:
    """Returns {id: name, arn: name} for all queues."""
    if snapshot:
        return snapshot.get("queues") or {}
    items = _paginate(client, "list_queues", "QueueSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {i["Id"]: {"id": i["Id"], "arn": i.get("Arn", ""), "name": i.get("Name", "")}
            for i in items}


def fetch_all_prompts(client, instance_id, snapshot) -> dict:
    if snapshot:
        return snapshot.get("prompts") or {}
    items = _paginate(client, "list_prompts", "PromptSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {i["Id"]: {"id": i["Id"], "arn": i.get("Arn", ""), "name": i.get("Name", "")}
            for i in items}


def fetch_all_hours(client, instance_id, snapshot) -> dict:
    if snapshot:
        return snapshot.get("hours_of_operation") or {}
    items = _paginate(client, "list_hours_of_operations", "HoursOfOperationSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    return {i["Id"]: {"id": i["Id"], "arn": i.get("Arn", ""), "name": i.get("Name", "")}
            for i in items}


def fetch_phone_number_flow_arns(client, instance_id) -> set:
    """Return set of contact-flow ARNs/IDs assigned to phone numbers."""
    items = _paginate(client, "list_phone_numbers_v2", "ListPhoneNumbersSummaryList",
                      InstanceId=instance_id, MaxResults=100)
    refs = set()
    for item in items:
        arn = item.get("TargetArn") or ""
        if "/contact-flow/" in arn:
            refs.add(arn)
            refs.add(arn.split("/")[-1])   # also add the bare ID
    return refs


def describe_flow_content(client, instance_id, flow_id) -> dict | None:
    try:
        raw     = client.describe_contact_flow(InstanceId=instance_id, ContactFlowId=flow_id)
        content = raw["ContactFlow"].get("Content") or ""
        return json.loads(content) if isinstance(content, str) else content
    except (ClientError, json.JSONDecodeError):
        return None


# ── Reference extraction ─────────────────────────────────────────────────────────

def _id_from_arn(arn: str) -> str:
    """Extract bare resource ID from an ARN (last path segment)."""
    return arn.split("/")[-1] if "/" in arn else arn


def _scan_arns(obj, connect_refs: dict, lambda_arns: set):
    """Recursively scan JSON for Connect and Lambda ARNs."""
    if isinstance(obj, str):
        for arn in _ARN_RE.findall(obj):
            if ":connect:" in arn:
                # Classify by resource type in path:
                # arn:aws:connect:region:acct:instance/id/<type>/<id>
                parts = arn.rstrip("/").split("/")
                if len(parts) >= 4:
                    rtype = parts[-2]
                    if rtype == "queue":
                        connect_refs["queues"].add(arn)
                        connect_refs["queues"].add(parts[-1])
                    elif rtype == "contact-flow":
                        connect_refs["flows"].add(arn)
                        connect_refs["flows"].add(parts[-1])
                    elif rtype == "prompt":
                        connect_refs["prompts"].add(arn)
                        connect_refs["prompts"].add(parts[-1])
                    elif rtype == "operating-hours":
                        connect_refs["hours"].add(arn)
                        connect_refs["hours"].add(parts[-1])
            elif ":lambda:" in arn:
                lambda_arns.add(arn)
    elif isinstance(obj, dict):
        for v in obj.values():
            _scan_arns(v, connect_refs, lambda_arns)
    elif isinstance(obj, list):
        for v in obj:
            _scan_arns(v, connect_refs, lambda_arns)


def extract_refs_from_content(content: dict) -> tuple:
    """
    Extract all resource references from a parsed flow content dict.
    Returns (connect_refs dict, lambda_arns set, referenced_flow_ids set).
    """
    connect_refs = {"queues": set(), "prompts": set(), "hours": set(), "flows": set()}
    lambda_arns: set = set()

    for action in (content.get("Actions") or []):
        atype  = action.get("Type", "")
        params = action.get("Parameters") or {}

        # Specific known fields — more reliable than ARN scan alone
        if atype == "InvokeLambdaFunction":
            arn = params.get("LambdaFunctionARN") or ""
            if arn:
                lambda_arns.add(arn)

        if atype == "SetQueue":
            qid = (params.get("Queue") or {}).get("Id") or params.get("QueueId") or ""
            if qid:
                connect_refs["queues"].add(qid)
                connect_refs["queues"].add(_id_from_arn(qid))

        if atype == "CheckHoursOfOperation":
            hid = (params.get("HoursOfOperation") or {}).get("Id") \
                  or params.get("HoursOfOperationId") or ""
            if hid:
                connect_refs["hours"].add(hid)
                connect_refs["hours"].add(_id_from_arn(hid))

        if atype in ("TransferContactToFlow", "InvokeFlowModule"):
            fid = (params.get("ContactFlow") or {}).get("Id") \
                  or params.get("ContactFlowId") or ""
            if fid:
                connect_refs["flows"].add(fid)
                connect_refs["flows"].add(_id_from_arn(fid))

        # Generic ARN scan catches anything the above misses
        _scan_arns(params, connect_refs, lambda_arns)

    return connect_refs, lambda_arns


# ── Lambda existence check ────────────────────────────────────────────────────────

def check_lambda_arns(lam_client, arns: set) -> dict:
    """
    For each ARN, call GetFunction. Returns {arn: True/False} for exists/missing.
    """
    results = {}
    total = len(arns)
    for i, arn in enumerate(sorted(arns), 1):
        print(f"\r  Checking Lambda {i}/{total}...", end="", flush=True, file=sys.stderr)
        try:
            lam_client.get_function(FunctionName=arn)
            results[arn] = True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ResourceNotFoundException", "404"):
                results[arn] = False
            else:
                results[arn] = True   # assume exists if we get a different error
    print(file=sys.stderr)
    return results


# ── Output ───────────────────────────────────────────────────────────────────────

def _hr(label: str = "", width: int = 72):
    if label:
        pad = width - len(label) - 4
        print(f"\n  ── {label} {'─' * max(pad, 0)}")
    else:
        print("  " + "─" * width)


def _name(resource: dict) -> str:
    return resource.get("name") or resource.get("username") or resource.get("id") or "?"


def print_human(report: dict):
    _hr()
    print(f"  ORPHANED RESOURCES   {report['instance_id']}")
    _hr()
    print(f"  {report['flows_scanned']} flow(s) scanned\n")

    # ── Orphaned flows ────────────────────────────────────────────────────────
    _hr("ORPHANED FLOWS — not called by any flow or phone number")
    items = report["orphaned_flows"]
    if not items:
        print("  (none)")
    else:
        name_w = max((len(f["name"]) for f in items), default=20)
        type_w = max((len(f["type"]) for f in items), default=12)
        for f in sorted(items, key=lambda x: x["name"].lower()):
            print(f"  \033[33m{f['name']:<{name_w}}\033[0m  {f['type']:<{type_w}}  {f['id']}")

    # ── Orphaned queues ───────────────────────────────────────────────────────
    _hr("ORPHANED QUEUES — exist in instance but not referenced in any flow")
    items = report["orphaned_queues"]
    if not items:
        print("  (none)")
    else:
        for r in sorted(items, key=lambda x: _name(x).lower()):
            print(f"  \033[33m{_name(r)}\033[0m")
            print(f"    {r.get('arn') or r.get('id', '?')}")

    # ── Orphaned prompts ──────────────────────────────────────────────────────
    _hr("ORPHANED PROMPTS — exist in instance but not referenced in any flow")
    items = report["orphaned_prompts"]
    if not items:
        print("  (none)")
    else:
        for r in sorted(items, key=lambda x: _name(x).lower()):
            print(f"  \033[33m{_name(r)}\033[0m")
            print(f"    {r.get('arn') or r.get('id', '?')}")

    # ── Orphaned hours ────────────────────────────────────────────────────────
    _hr("ORPHANED HOURS OF OPERATION — exist in instance but not referenced in any flow")
    items = report["orphaned_hours"]
    if not items:
        print("  (none)")
    else:
        for r in sorted(items, key=lambda x: _name(x).lower()):
            print(f"  \033[33m{_name(r)}\033[0m")
            print(f"    {r.get('arn') or r.get('id', '?')}")

    # ── Lambda references ─────────────────────────────────────────────────────
    lambda_check = report.get("lambda_check")
    if lambda_check is not None:
        broken  = [a for a, ok in lambda_check.items() if not ok]
        ok_arns = [a for a, ok in lambda_check.items() if ok]
        _hr("LAMBDA REFERENCES — referenced in flows")
        if broken:
            print(f"  \033[31m{len(broken)} broken reference(s):\033[0m")
            for arn in sorted(broken):
                print(f"    \033[31m✗  {arn}\033[0m")
        if ok_arns:
            print(f"  \033[32m{len(ok_arns)} verified:\033[0m")
            for arn in sorted(ok_arns):
                print(f"    \033[32m✓  {arn}\033[0m")
        if not broken and not ok_arns:
            print("  (no Lambda references found)")
    else:
        _hr("LAMBDA REFERENCES — referenced in flows (run --check-lambdas to verify)")
        arns = sorted(report.get("lambda_arns") or [])
        if not arns:
            print("  (none)")
        else:
            for arn in arns:
                print(f"  {arn}")

    print()
    _hr()

    # Summary
    totals = (len(report["orphaned_flows"]) + len(report["orphaned_queues"]) +
              len(report["orphaned_prompts"]) + len(report["orphaned_hours"]))
    broken_count = len([a for a, ok in (lambda_check or {}).items() if not ok])
    if totals == 0 and broken_count == 0:
        print("  \033[32m✓ No orphaned resources found\033[0m")
    else:
        print(f"  {totals} orphaned resource(s) found", end="")
        if broken_count:
            print(f"  ·  \033[31m{broken_count} broken Lambda reference(s)\033[0m", end="")
        print()
    print()


# ── CSV output ───────────────────────────────────────────────────────────────────

def write_csv(report: dict, path):
    import csv
    rows = []
    for f in report["orphaned_flows"]:
        rows.append({"category": "flow",   "name": f["name"],  "id": f["id"],
                     "type": f["type"],    "arn":  f.get("arn", ""), "note": "orphaned flow"})
    for r in report["orphaned_queues"]:
        rows.append({"category": "queue",  "name": _name(r),   "id": r.get("id", ""),
                     "type": "",           "arn":  r.get("arn", ""), "note": "orphaned queue"})
    for r in report["orphaned_prompts"]:
        rows.append({"category": "prompt", "name": _name(r),   "id": r.get("id", ""),
                     "type": "",           "arn":  r.get("arn", ""), "note": "orphaned prompt"})
    for r in report["orphaned_hours"]:
        rows.append({"category": "hours",  "name": _name(r),   "id": r.get("id", ""),
                     "type": "",           "arn":  r.get("arn", ""), "note": "orphaned hours"})
    lambda_check = report.get("lambda_check") or {}
    for arn, ok in lambda_check.items():
        if not ok:
            rows.append({"category": "lambda", "name": arn.split(":")[-1], "id": "",
                         "type": "", "arn": arn, "note": "broken lambda reference"})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["category", "name", "id", "type", "arn", "note"])
        w.writeheader()
        w.writerows(rows)


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args           = parse_args()
    connect, lam   = make_clients(args.region, args.profile)
    instance_id    = args.instance_id

    # Load snapshot for resource lists (optional)
    snapshot = ct_snapshot.load(instance_id)
    if snapshot:
        ct_snapshot.warn_if_stale(snapshot)
    else:
        print("  Note: no snapshot found — fetching resource lists via API.", file=sys.stderr)

    # Fetch resource inventories
    print("  Fetching resource lists...", file=sys.stderr)
    all_queues  = fetch_all_queues(connect, instance_id, snapshot)
    all_prompts = fetch_all_prompts(connect, instance_id, snapshot)
    all_hours   = fetch_all_hours(connect, instance_id, snapshot)

    # Fetch all flow summaries
    print("  Listing flows...", file=sys.stderr)
    all_flows = fetch_all_flows(connect, instance_id)
    flow_by_id = {f["Id"]: f for f in all_flows}

    # Phone number → flow assignments
    print("  Fetching phone number assignments...", file=sys.stderr)
    phone_flow_refs = fetch_phone_number_flow_arns(connect, instance_id)

    # Scan all flow content to build reference sets
    all_connect_refs = {"queues": set(), "prompts": set(), "hours": set(), "flows": set()}
    all_lambda_arns:  set = set()

    print(f"  Scanning {len(all_flows)} flow(s) for references...", file=sys.stderr)
    for i, summary in enumerate(all_flows, 1):
        print(f"\r  [{i}/{len(all_flows)}] {summary['Name'][:50]}", end="", flush=True,
              file=sys.stderr)
        content = describe_flow_content(connect, instance_id, summary["Id"])
        if content is None:
            continue
        crefs, larns = extract_refs_from_content(content)
        for k in all_connect_refs:
            all_connect_refs[k] |= crefs[k]
        all_lambda_arns |= larns
    print(file=sys.stderr)

    # Add phone number references to flow refs
    all_connect_refs["flows"] |= phone_flow_refs

    # ── Orphaned flows ────────────────────────────────────────────────────────
    orphaned_flows = []
    for fid, summary in flow_by_id.items():
        # Check by both ID and ARN
        farn = summary.get("Arn", "")
        if fid not in all_connect_refs["flows"] and farn not in all_connect_refs["flows"]:
            orphaned_flows.append({
                "id":   fid,
                "arn":  farn,
                "name": summary.get("Name", ""),
                "type": summary.get("ContactFlowType", ""),
            })

    # ── Orphaned queues ───────────────────────────────────────────────────────
    orphaned_queues = []
    for qid, q in all_queues.items():
        qarn = q.get("arn", "")
        if qid not in all_connect_refs["queues"] and qarn not in all_connect_refs["queues"]:
            orphaned_queues.append(q)

    # ── Orphaned prompts ──────────────────────────────────────────────────────
    orphaned_prompts = []
    for pid, p in all_prompts.items():
        parn = p.get("arn", "")
        if pid not in all_connect_refs["prompts"] and parn not in all_connect_refs["prompts"]:
            orphaned_prompts.append(p)

    # ── Orphaned hours ────────────────────────────────────────────────────────
    orphaned_hours = []
    for hid, h in all_hours.items():
        harn = h.get("arn", "")
        if hid not in all_connect_refs["hours"] and harn not in all_connect_refs["hours"]:
            orphaned_hours.append(h)

    # ── Lambda check (optional) ───────────────────────────────────────────────
    lambda_check = None
    if args.check_lambdas and all_lambda_arns:
        print(f"  Verifying {len(all_lambda_arns)} Lambda ARN(s)...", file=sys.stderr)
        lambda_check = check_lambda_arns(lam, all_lambda_arns)

    report = {
        "instance_id":      instance_id,
        "flows_scanned":    len(all_flows),
        "orphaned_flows":   orphaned_flows,
        "orphaned_queues":  orphaned_queues,
        "orphaned_prompts": orphaned_prompts,
        "orphaned_hours":   orphaned_hours,
        "lambda_arns":      sorted(all_lambda_arns),
        "lambda_check":     lambda_check,
    }

    if args.output_json:
        print(json.dumps(report, indent=2))
        return

    print_human(report)

    if args.csv:
        dest = ct_snapshot.output_path("orphaned_resources", args.csv)
        write_csv(report, dest)
        print(f"  Saved → {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
