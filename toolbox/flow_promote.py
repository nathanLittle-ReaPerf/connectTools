#!/usr/bin/env python3
"""flow_promote.py — Promote Amazon Connect contact flows from Dev to Prod.

Exports flows from a Dev instance, remaps all resource ARNs (queues, prompts,
other flows, Lambdas, etc.) to their Prod equivalents by name-matching, then
imports them into Prod. Detects sub-flow dependencies interactively — the user
decides whether to deploy or skip each one. A full summary is printed at the end.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
import ct_snapshot

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

# ── Terminal colors ───────────────────────────────────────────────────────────
BOLD   = "\033[1m"
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
DIM    = "\033[2m"
RESET  = "\033[0m"

# ── ARN classification ────────────────────────────────────────────────────────
_ARN_RE = re.compile(
    r'arn:aws[a-z0-9-]*:[a-zA-Z0-9-]+:[a-z0-9-]*:\d*:[^\s\'">,\]\}]+'
)

# Connect ARN path segment → (snapshot key, kind label)
_CONNECT_PATH: dict[str, tuple[str, str]] = {
    "/contact-flow/":        ("flows",              "flow"),
    "/queue/":               ("queues",             "queue"),
    "/operating-hours/":     ("hours_of_operation", "hours_of_operation"),
    "/prompt/":              ("prompts",            "prompt"),
    "/transfer-destination/":("quick_connects",     "quick_connect"),
}
_KIND_TO_SNAP_KEY = {kind: sk for _, (sk, kind) in _CONNECT_PATH.items()}


def extract_arns(text: str) -> set[str]:
    return set(_ARN_RE.findall(text))


def classify_arn(arn: str) -> str | None:
    if "connect" in arn:
        for seg, (_, kind) in _CONNECT_PATH.items():
            if seg in arn:
                return kind
        # Bare instance ARN (no resource sub-path)
        if "/instance/" in arn:
            return "instance"
    elif ":lambda:" in arn and ":function:" in arn:
        return "lambda"
    elif ":lex:" in arn or ":lexv2:" in arn:
        return "lex"
    return None


def arn_id(arn: str) -> str:
    """Last path segment of an ARN (the resource UUID)."""
    return arn.rstrip("/").split("/")[-1]


def remap_lambda_arn(dev_arn: str, prod_account: str, prod_region: str) -> str:
    """Swap region (index 3) and account (index 4) in a Lambda ARN."""
    parts = dev_arn.split(":")
    if len(parts) >= 5:
        parts[3] = prod_region
        parts[4] = prod_account
    return ":".join(parts)


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def name_to_resource(snapshot: dict, resource_type: str, name: str) -> dict | None:
    """Exact (case-insensitive) name lookup in a snapshot resource dict."""
    needle = name.lower()
    for item in snapshot.get(resource_type, {}).values():
        if (item.get("name") or item.get("username") or "").lower() == needle:
            return item
    return None


# ── Dev export ────────────────────────────────────────────────────────────────

def _list_flows(connect, instance_id: str) -> list[dict]:
    flows, token = [], None
    while True:
        kwargs: dict = dict(InstanceId=instance_id, MaxResults=100)
        if token:
            kwargs["NextToken"] = token
        resp  = connect.list_contact_flows(**kwargs)
        flows.extend(resp.get("ContactFlowSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return flows


def export_flow(connect, instance_id: str, name: str) -> dict | None:
    """Export a flow from Connect by exact name. Returns data dict or None."""
    matches = [
        f for f in _list_flows(connect, instance_id)
        if f["Name"].lower() == name.lower()
        and f.get("ContactFlowState") == "ACTIVE"
    ]
    if not matches:
        return None
    summary = matches[0]
    try:
        cf = connect.describe_contact_flow(
            InstanceId=instance_id, ContactFlowId=summary["Id"]
        )["ContactFlow"]
    except ClientError as e:
        print(f"  {RED}Error describing '{name}': {e.response['Error']['Message']}{RESET}",
              file=sys.stderr)
        return None
    return {
        "name":        cf["Name"],
        "type":        cf.get("Type", summary.get("ContactFlowType", "")),
        "arn":         cf.get("Arn", summary.get("Arn", "")),
        "id":          cf.get("Id", summary["Id"]),
        "description": cf.get("Description", ""),
        "content_str": cf.get("Content", ""),
    }


# ── Dependency analysis ───────────────────────────────────────────────────────

def find_dep_flow_arns(content_str: str) -> set[str]:
    return {a for a in extract_arns(content_str) if classify_arn(a) == "flow"}


def dep_name(dep_arn: str, dev_snap: dict) -> str:
    item = dev_snap.get("flows", {}).get(arn_id(dep_arn), {})
    return item.get("name") or f"[unknown:{arn_id(dep_arn)[:8]}]"


# ── ARN remapping ─────────────────────────────────────────────────────────────

def build_remap(
    content_str: str,
    dev_snap: dict,
    prod_snap: dict,
    dev_instance_id: str,
    prod_instance_id: str,
    prod_account: str,
    prod_region: str,
) -> tuple[dict[str, str], list[dict]]:
    """
    Returns (remap_table, unresolved_list).
      remap_table  — {dev_arn: prod_arn}  for every resolvable ARN
      unresolved   — [{arn, type, name?, reason}] for anything that couldn't be mapped
    """
    remap: dict[str, str] = {}
    unresolved: list[dict] = []

    for arn in extract_arns(content_str):
        if arn in remap:
            continue
        kind = classify_arn(arn)
        if kind is None:
            continue

        if kind == "instance":
            remap[arn] = arn.replace(dev_instance_id, prod_instance_id)

        elif kind in _KIND_TO_SNAP_KEY:
            snap_key  = _KIND_TO_SNAP_KEY[kind]
            dev_item  = dev_snap.get(snap_key, {}).get(arn_id(arn))
            if not dev_item:
                unresolved.append({"arn": arn, "type": kind,
                                   "reason": "not in Dev snapshot (run instance_snapshot.py --refresh)"})
                continue
            name      = dev_item.get("name", "")
            prod_item = name_to_resource(prod_snap, snap_key, name)
            if prod_item:
                remap[arn] = prod_item["arn"]
            else:
                unresolved.append({"arn": arn, "type": kind, "name": name,
                                   "reason": f'"{name}" not found in Prod snapshot'})

        elif kind == "lambda":
            remap[arn] = remap_lambda_arn(arn, prod_account, prod_region)

        elif kind == "lex":
            unresolved.append({"arn": arn, "type": "lex",
                               "reason": "Lex bot ARNs require manual mapping"})

    return remap, unresolved


def apply_remap(content_str: str, remap: dict[str, str]) -> str:
    # Longest-first to prevent partial replacements
    for dev_arn in sorted(remap, key=len, reverse=True):
        content_str = content_str.replace(dev_arn, remap[dev_arn])
    return content_str


# ── Prod deploy ───────────────────────────────────────────────────────────────

def _find_prod_flow(connect, instance_id: str, name: str) -> dict | None:
    matches = [f for f in _list_flows(connect, instance_id)
               if f["Name"].lower() == name.lower()]
    return matches[0] if matches else None


def _backup(connect, instance_id: str, flow_id: str, name: str, backup_dir: Path) -> Path:
    cf = connect.describe_contact_flow(
        InstanceId=instance_id, ContactFlowId=flow_id
    )["ContactFlow"]
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\-]", "_", name)
    path = backup_dir / f"{safe}_{ts}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "metadata": {
                "name": cf.get("Name"),
                "id":   cf.get("Id"),
                "arn":  cf.get("Arn"),
                "type": cf.get("Type"),
                "backed_up_at": ts,
            },
            "content": json.loads(cf.get("Content", "{}")),
        }, fh, indent=2)
    return path


def deploy_to_prod(
    connect,
    instance_id: str,
    name: str,
    flow_type: str,
    description: str,
    content_str: str,
    publish: bool,
    backup_dir: Path | None,
    dry_run: bool,
) -> dict:
    existing = _find_prod_flow(connect, instance_id, name)
    result: dict = {"action": None, "arn": None, "id": None, "backed_up_to": None}

    if dry_run:
        result["action"] = "UPDATE" if existing else "CREATE"
        result["arn"]    = existing["Arn"] if existing else "(new)"
        return result

    if existing:
        flow_id = existing["Id"]
        if backup_dir:
            result["backed_up_to"] = str(_backup(connect, instance_id, flow_id, name, backup_dir))
        connect.update_contact_flow_content(
            InstanceId=instance_id, ContactFlowId=flow_id, Content=content_str
        )
        if publish:
            connect.publish_contact_flow(InstanceId=instance_id, ContactFlowId=flow_id)
        result.update({"action": "UPDATED", "arn": existing["Arn"], "id": flow_id})
    else:
        resp    = connect.create_contact_flow(
            InstanceId=instance_id, Name=name, Type=flow_type,
            Description=description or "", Content=content_str,
        )
        cf      = resp["ContactFlow"]
        flow_id = cf["Id"]
        if publish:
            connect.publish_contact_flow(InstanceId=instance_id, ContactFlowId=flow_id)
        result.update({"action": "CREATED", "arn": cf["Arn"], "id": flow_id})

    return result


# ── Snapshot refresh ──────────────────────────────────────────────────────────

def refresh_snapshot(connect, instance_id: str, label: str) -> dict:
    from instance_snapshot import fetch_snapshot  # local import to avoid circular deps
    print(f"  Refreshing {label} snapshot...", file=sys.stderr)
    snap = fetch_snapshot(connect, instance_id)
    ct_snapshot.save(instance_id, snap)
    return snap


# ── Topological sort ──────────────────────────────────────────────────────────

def topo_sort(to_deploy: dict[str, dict], dev_snap: dict) -> list[str]:
    """Return a deploy order where dependencies come before dependents."""
    dev_flows = dev_snap.get("flows", {})

    # graph[name] = names in to_deploy that this flow depends on
    graph: dict[str, set[str]] = {n: set() for n in to_deploy}
    for name, flow in to_deploy.items():
        for dep_arn in find_dep_flow_arns(flow["content_str"]):
            dname = dev_flows.get(arn_id(dep_arn), {}).get("name", "")
            if dname in to_deploy:
                graph[name].add(dname)

    in_degree = {n: len(deps) for n, deps in graph.items()}
    ready     = deque(sorted(n for n, d in in_degree.items() if d == 0))
    order: list[str] = []

    while ready:
        n = ready.popleft()
        order.append(n)
        for other, deps in graph.items():
            if n in deps:
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    ready.append(other)

    # Cycles (shouldn't occur in real flows) — append whatever remains
    order += [n for n in to_deploy if n not in order]
    return order


# ── Interactive helpers ───────────────────────────────────────────────────────

def ask_yn(prompt: str, default: bool = True) -> bool:
    opts = "Y/n" if default else "y/N"
    while True:
        try:
            ans = input(f"{prompt} [{opts}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


# ── CLI ───────────────────────────────────────────────────────────────────────

_MAN = """\
NAME
    flow_promote.py — Promote Amazon Connect contact flows from Dev to Prod

SYNOPSIS
    python flow_promote.py --dev-instance-id UUID --prod-instance-id UUID
                           --name NAME [--name NAME ...] [OPTIONS]

DESCRIPTION
    Exports contact flows from a Dev Connect instance, remaps all embedded ARNs
    (queues, prompts, flows, Lambdas, hours-of-operation, quick-connects) to
    their Prod equivalents by name-matching, and imports them into Prod.

    Sub-flow dependencies are detected automatically. For each dependency not
    already in the promotion list, the tool asks the user to deploy or skip it.
    Flows are deployed in dependency order (leaves first). A summary of deployed
    and skipped flows is printed at the end.

    ARN remapping strategy:
      Connect resources  name-matched from snapshots (instance_snapshot.py)
      Lambda functions   account ID + region swapped; function name preserved
      Lex bots           flagged as unresolvable (require manual mapping)

OPTIONS
    --dev-instance-id UUID    Dev Connect instance ID (required)
    --prod-instance-id UUID   Prod Connect instance ID (required)
    --name NAME               Flow name to promote; exact match, case-insensitive.
                              Repeatable: --name "Flow A" --name "Flow B"
    --dev-region REGION       Dev AWS region (default: us-east-1)
    --prod-region REGION      Prod AWS region (default: same as --dev-region)
    --dev-profile NAME        AWS profile for Dev credentials
    --prod-profile NAME       AWS profile for Prod credentials
    --publish                 Publish flows after importing (default: leave as draft)
    --backup-dir PATH         Directory for Prod backups (default: ./flow_backups)
    --no-backup               Skip backing up Prod flows before overwriting
    --dry-run                 Show plan without making any changes
    --skip-unresolved         Deploy even when some ARNs cannot be remapped
    --refresh-snapshots       Fetch fresh snapshots before starting

PREREQUISITES
    Snapshots for both instances must exist:
      python instance_snapshot.py --instance-id <dev-id>  --region <region>
      python instance_snapshot.py --instance-id <prod-id> --region <region>

    Or pass --refresh-snapshots to fetch them automatically.

IAM (Dev)
    connect:ListContactFlows, connect:DescribeContactFlow

IAM (Prod)
    connect:ListContactFlows, connect:DescribeContactFlow
    connect:CreateContactFlow, connect:UpdateContactFlowContent
    connect:PublishContactFlow  (only with --publish)
    sts:GetCallerIdentity

EXAMPLES
    # Dry run — see what would happen
    python flow_promote.py --dev-instance-id <dev> --prod-instance-id <prod> \\
        --name "Main IVR" --dry-run

    # Promote two flows and publish
    python flow_promote.py --dev-instance-id <dev> --prod-instance-id <prod> \\
        --name "Main IVR" --name "Auth Sub-Flow" --publish

    # Different AWS profiles for Dev and Prod accounts
    python flow_promote.py --dev-instance-id <dev> --prod-instance-id <prod> \\
        --name "Main IVR" --dev-profile dev-admin --prod-profile prod-admin \\
        --publish --refresh-snapshots
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Promote contact flows from Dev to Prod with ARN remapping.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dev-instance-id",  required=True, metavar="UUID")
    p.add_argument("--prod-instance-id", required=True, metavar="UUID")
    p.add_argument("--name", action="append", required=True, dest="names",
                   metavar="NAME", help="Flow name to promote (repeatable)")
    p.add_argument("--dev-region",  default="us-east-1")
    p.add_argument("--prod-region", default=None,
                   help="Defaults to --dev-region if not specified")
    p.add_argument("--dev-profile",  default=None)
    p.add_argument("--prod-profile", default=None)
    p.add_argument("--publish",  action="store_true",
                   help="Publish flows after importing (default: leave as draft)")
    p.add_argument("--backup-dir", default="./flow_backups",
                   help="Where to save Prod backups (default: ./flow_backups)")
    p.add_argument("--no-backup",  action="store_true",
                   help="Skip backing up Prod flows before overwriting")
    p.add_argument("--dry-run",    action="store_true",
                   help="Show what would happen without making changes")
    p.add_argument("--skip-unresolved", action="store_true",
                   help="Deploy even when some ARNs cannot be remapped (with warnings)")
    p.add_argument("--refresh-snapshots", action="store_true",
                   help="Fetch fresh snapshots before starting")
    return p.parse_args()


def main() -> None:
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args        = parse_args()
    prod_region = args.prod_region or args.dev_region

    # ── Clients ───────────────────────────────────────────────────────────────
    dev_session  = boto3.Session(profile_name=args.dev_profile,  region_name=args.dev_region)
    prod_session = boto3.Session(profile_name=args.prod_profile, region_name=prod_region)
    dev_connect  = dev_session.client("connect", config=RETRY_CONFIG)
    prod_connect = prod_session.client("connect", config=RETRY_CONFIG)
    prod_account = prod_session.client("sts", config=RETRY_CONFIG).get_caller_identity()["Account"]

    # ── Snapshots ─────────────────────────────────────────────────────────────
    print("Loading snapshots...")
    if args.refresh_snapshots:
        dev_snap  = refresh_snapshot(dev_connect,  args.dev_instance_id,  "Dev")
        prod_snap = refresh_snapshot(prod_connect, args.prod_instance_id, "Prod")
    else:
        dev_snap  = ct_snapshot.load(args.dev_instance_id)
        prod_snap = ct_snapshot.load(args.prod_instance_id)
        if dev_snap is None:
            print(f"{RED}No Dev snapshot found. Run:{RESET}\n"
                  f"  python instance_snapshot.py --instance-id {args.dev_instance_id}"
                  f" --region {args.dev_region}")
            sys.exit(1)
        if prod_snap is None:
            print(f"{RED}No Prod snapshot found. Run:{RESET}\n"
                  f"  python instance_snapshot.py --instance-id {args.prod_instance_id}"
                  f" --region {prod_region}"
                  + (f" --profile {args.prod_profile}" if args.prod_profile else ""))
            sys.exit(1)
        ct_snapshot.warn_if_stale(dev_snap)
        ct_snapshot.warn_if_stale(prod_snap)

    # ── Backup dir ────────────────────────────────────────────────────────────
    backup_dir: Path | None = None
    if not args.no_backup and not args.dry_run:
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)

    # Name sets for fast membership checks (updated as we deploy new flows)
    prod_flow_names: set[str] = {
        item.get("name", "").lower()
        for item in prod_snap.get("flows", {}).values()
    }
    dev_flow_names: set[str] = {
        item.get("name", "").lower()
        for item in dev_snap.get("flows", {}).values()
    }

    # ── Interactive dependency resolution ─────────────────────────────────────
    to_deploy:   dict[str, dict]        = {}   # name → exported flow data
    skipped:     set[str]               = set()
    dep_missing: dict[str, list[str]]   = {}   # parent → [dep names skipped + not in Prod]
    queue:       deque[str]             = deque(args.names)
    visited:     set[str]               = set()

    print()
    while queue:
        name = queue.popleft()
        if name in visited:
            continue
        visited.add(name)
        if name in skipped:
            continue

        print(f"{BOLD}[{name}]{RESET}")

        flow = export_flow(dev_connect, args.dev_instance_id, name)
        if flow is None:
            print(f"  {RED}✗ Not found in Dev — skipping{RESET}")
            skipped.add(name)
            print()
            continue

        dep_arns = find_dep_flow_arns(flow["content_str"])
        problems: list[str] = []

        for dep_arn in sorted(dep_arns):
            dname = dep_name(dep_arn, dev_snap)
            if dname in to_deploy or dname in visited:
                continue  # already queued or processed
            if dname in skipped:
                if dname.lower() not in prod_flow_names:
                    problems.append(dname)
                continue

            in_prod = dname.lower() in prod_flow_names
            in_dev  = dname.lower() in dev_flow_names

            if in_prod:
                print(f"  {YELLOW}→ depends on \"{dname}\" [exists in Prod — may be outdated]{RESET}")
                if ask_yn(f'    Promote updated Dev version of "{dname}" too?', default=False):
                    queue.append(dname)
                # Either way, remap will succeed — Prod ARN exists
            elif in_dev:
                print(f"  {RED}→ depends on \"{dname}\" [NOT in Prod]{RESET}")
                if ask_yn(f'    "{dname}" doesn\'t exist in Prod — deploy it?', default=True):
                    queue.append(dname)
                else:
                    skipped.add(dname)
                    problems.append(dname)
            else:
                print(f"  {RED}→ depends on \"{dname}\" [NOT in Prod OR Dev — broken reference]{RESET}")
                problems.append(dname)

        to_deploy[name] = flow
        if problems:
            dep_missing[name] = problems
        print()

    if not to_deploy:
        print("Nothing to deploy.")
        return

    # ── Deployment plan ───────────────────────────────────────────────────────
    deploy_order = topo_sort(to_deploy, dev_snap)

    dry_tag = "DRY RUN — " if args.dry_run else ""
    print(f"{BOLD}{'═' * 52}{RESET}")
    print(f"{BOLD}  {dry_tag}DEPLOYMENT PLAN{RESET}")
    print(f"{BOLD}{'═' * 52}{RESET}")
    print(f"  Dev:              {args.dev_instance_id}")
    print(f"  Prod:             {args.prod_instance_id}")
    print(f"  Publish on import: {'yes' if args.publish else 'no  (flows will be DRAFT)'}")
    print(f"  Backups:          {'disabled' if args.no_backup or args.dry_run else str(backup_dir)}")
    print()
    print(f"  Flows to deploy ({len(deploy_order)}):")
    for n in deploy_order:
        action = "UPDATE" if n.lower() in prod_flow_names else "CREATE"
        warn   = f"  {YELLOW}⚠ dep issues{RESET}" if n in dep_missing else ""
        print(f"    {action:<6}  {n}{warn}")
    if skipped:
        print(f"\n  Flows to skip ({len(skipped)}):")
        for s in sorted(skipped):
            print(f"    {DIM}SKIP    {s}{RESET}")
    print()

    if not args.dry_run:
        if not ask_yn("Proceed with deployment?", default=True):
            print("Aborted.")
            return
        print()

    # ── Deploy ────────────────────────────────────────────────────────────────
    results: list[dict] = []

    for name in deploy_order:
        flow = to_deploy[name]
        print(f"{BOLD}Deploying: {name}{RESET}")

        remap, unresolved = build_remap(
            flow["content_str"],
            dev_snap, prod_snap,
            args.dev_instance_id, args.prod_instance_id,
            prod_account, prod_region,
        )

        for u in unresolved:
            label = u.get("name") or u["arn"][:60]
            print(f"  {YELLOW}⚠  Unresolved {u['type']}: {label} — {u['reason']}{RESET}")

        if unresolved and not args.skip_unresolved:
            print(f"  {RED}✗ Skipping \"{name}\" — unresolved ARNs present.\n"
                  f"    Fix the issues above or re-run with --skip-unresolved.{RESET}")
            skipped.add(name)
            results.append({"name": name, "status": "SKIPPED",
                            "reason": "unresolved ARNs", "unresolved": unresolved,
                            "dep_missing": dep_missing.get(name, [])})
            print()
            continue

        remapped = apply_remap(flow["content_str"], remap)

        # Sanity check — ensure remap didn't produce invalid JSON
        try:
            json.loads(remapped)
        except json.JSONDecodeError as e:
            print(f"  {RED}✗ Remapped content is not valid JSON: {e}{RESET}")
            results.append({"name": name, "status": "FAILED",
                            "reason": f"invalid JSON after remap: {e}",
                            "unresolved": unresolved, "dep_missing": dep_missing.get(name, [])})
            print()
            continue

        try:
            deploy_result = deploy_to_prod(
                prod_connect, args.prod_instance_id,
                name, flow["type"], flow["description"],
                remapped, args.publish, backup_dir, args.dry_run,
            )
        except ClientError as e:
            msg = e.response["Error"]["Message"]
            print(f"  {RED}✗ Deploy failed: {msg}{RESET}")
            results.append({"name": name, "status": "FAILED", "reason": msg,
                            "unresolved": unresolved, "dep_missing": dep_missing.get(name, [])})
            print()
            continue

        action   = deploy_result["action"]
        prod_arn = deploy_result["arn"] or ""
        print(f"  {GREEN}✓ {action}{RESET}  {prod_arn}")
        if deploy_result["backed_up_to"]:
            print(f"  {DIM}  Backup → {deploy_result['backed_up_to']}{RESET}")
        if args.publish and not args.dry_run:
            print(f"  {DIM}  Published{RESET}")

        # Register new Prod ARN so subsequent flows can remap references to this one
        if prod_arn and prod_arn != "(new)":
            flow_id = deploy_result["id"] or arn_id(prod_arn)
            prod_snap.setdefault("flows", {})[flow_id] = {
                "id": flow_id, "arn": prod_arn, "name": name, "type": flow["type"],
            }
            prod_flow_names.add(name.lower())

        results.append({
            "name":        name,
            "status":      action,
            "arn":         prod_arn,
            "unresolved":  unresolved,
            "dep_missing": dep_missing.get(name, []),
            "backed_up_to": deploy_result["backed_up_to"],
        })
        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    deployed  = [r for r in results if r["status"] not in ("SKIPPED", "FAILED")]
    failed    = [r for r in results if r["status"] in ("SKIPPED", "FAILED")]
    # Flows skipped during the dep loop that never made it into results
    extra_skipped = skipped - {r["name"] for r in results}

    print(f"{BOLD}{'═' * 52}{RESET}")
    print(f"{BOLD}  {dry_tag}SUMMARY{RESET}")
    print(f"{BOLD}{'═' * 52}{RESET}")

    if deployed:
        pub_note = "[PUBLISHED]" if (args.publish and not args.dry_run) else "[DRAFT]"
        print(f"\n{GREEN}DEPLOYED — review recommended:{RESET}")
        for r in deployed:
            status_str = f"{r['status']:<7} {pub_note}"
            print(f"  ✓  {r['name']:<46} {status_str}")
            for u in r.get("unresolved", []):
                lbl = u.get("name") or u["arn"][:50]
                print(f"       {YELLOW}⚠  Unresolved {u['type']}: {lbl}{RESET}")
            for dm in r.get("dep_missing", []):
                print(f"       {YELLOW}⚠  Dep \"{dm}\" was skipped — may fail at runtime{RESET}")

    if failed or extra_skipped:
        print(f"\n{RED}SKIPPED / FAILED — manual action needed:{RESET}")
        for r in failed:
            print(f"  ✗  {r['name']:<46} {r['status']}")
            if r.get("reason"):
                print(f"       {DIM}{r['reason']}{RESET}")
            for u in r.get("unresolved", []):
                lbl = u.get("name") or u["arn"][:50]
                print(f"       {YELLOW}⚠  Unresolved {u['type']}: {lbl}{RESET}")
        for s in sorted(extra_skipped):
            refs    = [n for n, deps in dep_missing.items() if s in deps]
            ref_str = f"referenced by: {', '.join(refs)}" if refs else "user skipped"
            print(f"  ✗  {s:<46} SKIPPED ({ref_str})")

    if not args.publish and not args.dry_run and deployed:
        print(f"\n{YELLOW}Flows are in DRAFT state. Publish in the Connect console or "
              f"re-run with --publish.{RESET}")
    if args.dry_run:
        print(f"\n{DIM}Dry run — no changes were made.{RESET}")
    print()


if __name__ == "__main__":
    main()
