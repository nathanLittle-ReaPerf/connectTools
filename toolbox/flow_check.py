#!/usr/bin/env python3
"""flow_check.py — Compare contact flows across regions and accounts.

Verify flow parity: quick hash comparison, detailed diffs, or full inventory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from io import StringIO
from pathlib import Path
from typing import NamedTuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Add lib directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import flow_compare as fc

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})


class Instance(NamedTuple):
    instance_id: str
    region: str
    label: str


class FlowInfo(NamedTuple):
    name: str
    id: str
    content_hash: str
    arn: str


def parse_instance_spec(spec: str) -> Instance:
    """Parse 'UUID:region:label' format."""
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError(f"Invalid instance spec: {spec}. Use 'UUID:region' or 'UUID:region:label'")

    instance_id = parts[0]
    region = parts[1]
    label = parts[2] if len(parts) > 2 else f"{region}"

    return Instance(instance_id, region, label)


def make_client(instance_id: str, region: str, profile: str | None = None):
    """Create a Connect client."""
    session = boto3.Session(profile_name=profile)
    return session.client("connect", region_name=region, config=RETRY_CONFIG)


def fetch_flow(client, instance_id: str, flow_name: str) -> dict | None:
    """Fetch a flow by name from an instance."""
    try:
        # List flows to find by name
        paginator = client.get_paginator("list_contact_flows")
        for page in paginator.paginate(InstanceId=instance_id, MaxResults=100):
            for flow in page.get("ContactFlowSummaryList", []):
                if flow["Name"].lower() == flow_name.lower():
                    # Found it, fetch full content
                    resp = client.describe_contact_flow(
                        InstanceId=instance_id,
                        ContactFlowId=flow["Id"]
                    )
                    return {
                        "id": flow["Id"],
                        "name": flow["Name"],
                        "arn": flow.get("Arn", ""),
                        "content": resp["ContactFlow"]["Content"],
                    }
        return None
    except ClientError as e:
        print(f"Error fetching flow from {instance_id}: {e}", file=sys.stderr)
        return None


def hash_flow_content(content: str) -> str:
    """Generate SHA256 hash of flow content."""
    if isinstance(content, dict):
        content = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def fetch_all_flows(client, instance_id: str) -> list[FlowInfo]:
    """Fetch all flows from an instance."""
    flows = []
    try:
        paginator = client.get_paginator("list_contact_flows")
        for page in paginator.paginate(InstanceId=instance_id, MaxResults=100):
            for flow in page.get("ContactFlowSummaryList", []):
                # Fetch content
                resp = client.describe_contact_flow(
                    InstanceId=instance_id,
                    ContactFlowId=flow["Id"]
                )
                content = resp["ContactFlow"]["Content"]
                content_hash = hash_flow_content(content)
                flows.append(FlowInfo(
                    name=flow["Name"],
                    id=flow["Id"],
                    content_hash=content_hash,
                    arn=flow.get("Arn", ""),
                ))
        return flows
    except ClientError as e:
        print(f"Error fetching flows from {instance_id}: {e}", file=sys.stderr)
        return []


def quick_check(instances: list[Instance], flow_name: str, profile: str | None = None) -> dict:
    """Quick hash comparison of a flow across instances. Returns results dict."""
    flows_by_hash = {}
    results = []

    for inst in instances:
        client = make_client(inst.instance_id, inst.region, profile)
        flow = fetch_flow(client, inst.instance_id, flow_name)

        if not flow:
            results.append({
                "label": inst.label,
                "region": inst.region,
                "flow_name": flow_name,
                "hash": "NOT FOUND",
                "status": "MISSING",
            })
            continue

        content_hash = hash_flow_content(flow["content"])
        results.append({
            "label": inst.label,
            "region": inst.region,
            "flow_name": flow_name,
            "hash": content_hash,
            "status": "",
        })

        if content_hash not in flows_by_hash:
            flows_by_hash[content_hash] = []
        flows_by_hash[content_hash].append(flow)

    # Assign status
    for result in results:
        if result["status"]:
            continue

        matching_count = len([f for f in flows_by_hash.get(result["hash"], [])])
        if matching_count == len(instances):
            result["status"] = "MATCH"
        elif matching_count > 1:
            result["status"] = "MATCH"
        else:
            result["status"] = "DIFF"

    unique_hashes = len(flows_by_hash)
    return {
        "mode": "quick",
        "flow_name": flow_name,
        "results": results,
        "summary": {
            "total_instances": len(instances),
            "unique_hashes": unique_hashes,
            "all_match": unique_hashes == 1,
        }
    }


def detail_check(instances: list[Instance], flow_name: str, profile: str | None = None) -> dict:
    """Detailed comparison with block diffs. Returns results dict."""
    quick_results = quick_check(instances, flow_name, profile)

    # Fetch all flows
    flows = {}
    for inst in instances:
        client = make_client(inst.instance_id, inst.region, profile)
        flow = fetch_flow(client, inst.instance_id, flow_name)
        if flow:
            flows[inst.label] = flow

    comparisons = []
    if len(flows) >= 2:
        # Compare pairs
        labels = list(flows.keys())
        for i in range(len(labels) - 1):
            for j in range(i + 1, len(labels)):
                label_a, label_b = labels[i], labels[j]
                flow_a, flow_b = flows[label_a], flows[label_b]

                # Parse content
                content_a = flow_a["content"] if isinstance(flow_a["content"], dict) else json.loads(flow_a["content"])
                content_b = flow_b["content"] if isinstance(flow_b["content"], dict) else json.loads(flow_b["content"])

                blocks_a = content_a.get("Blocks", [])
                blocks_b = content_b.get("Blocks", [])

                comparisons.append({
                    "instance_a": label_a,
                    "instance_b": label_b,
                    "identical": content_a == content_b,
                    "blocks_a": len(blocks_a),
                    "blocks_b": len(blocks_b),
                    "block_diff": abs(len(blocks_a) - len(blocks_b)),
                })

    return {
        "mode": "detail",
        "flow_name": flow_name,
        "results": quick_results["results"],
        "comparisons": comparisons,
        "summary": quick_results["summary"],
    }


def inventory_check(instances: list[Instance], profile: str | None = None) -> dict:
    """Show all flows across instances. Returns results dict."""
    all_flows = {}

    for inst in instances:
        client = make_client(inst.instance_id, inst.region, profile)
        flows = fetch_all_flows(client, inst.instance_id)

        for flow in flows:
            if flow.name not in all_flows:
                all_flows[flow.name] = {}
            all_flows[flow.name][inst.label] = {
                "hash": flow.content_hash,
                "id": flow.id,
                "arn": flow.arn,
            }

    # Build results
    results = []
    for flow_name in sorted(all_flows.keys()):
        hashes_by_instance = all_flows[flow_name]
        unique_hashes = len(set(h["hash"] for h in hashes_by_instance.values()))

        for instance_label, flow_data in sorted(hashes_by_instance.items()):
            results.append({
                "flow_name": flow_name,
                "instance": instance_label,
                "hash": flow_data["hash"],
                "flow_id": flow_data["id"],
                "arn": flow_data["arn"],
                "match_status": "MATCH" if unique_hashes == 1 else "DIFF",
            })

    return {
        "mode": "inventory",
        "results": results,
        "summary": {
            "total_instances": len(instances),
            "total_unique_flows": len(all_flows),
        }
    }


def print_quick_check(data: dict):
    """Print quick_check results in human-readable format."""
    print()
    print(f"Flow: {data['flow_name']}")
    print("─" * 80)
    print(f"{'Label':<20} {'Region':<15} {'Hash':<20} {'Status':<15}")
    print("─" * 80)
    for result in data["results"]:
        status_display = {
            "MATCH": "✓ Match",
            "DIFF": "✗ DIFF",
            "MISSING": "✗ MISSING",
        }.get(result["status"], result["status"])
        print(f"{result['label']:<20} {result['region']:<15} {result['hash']:<20} {status_display:<15}")
    print()

    summary = data["summary"]
    if summary["all_match"]:
        print("✓ All flows are identical!")
    else:
        print(f"✗ Found {summary['unique_hashes']} different version(s)")
    print()


def print_detail_check(data: dict):
    """Print detail_check results in human-readable format."""
    print_quick_check({
        "flow_name": data["flow_name"],
        "results": data["results"],
        "summary": data["summary"],
    })

    for comp in data["comparisons"]:
        if comp["identical"]:
            print(f"✓ {comp['instance_a']} ↔ {comp['instance_b']}: Identical")
        else:
            print(f"\n✗ {comp['instance_a']} ↔ {comp['instance_b']}: DIFFERENCES")
            print("─" * 60)
            print(f"  {comp['instance_a']}: {comp['blocks_a']} blocks")
            print(f"  {comp['instance_b']}: {comp['blocks_b']} blocks")
            if comp["block_diff"] > 0:
                print(f"  ⚠ Block count differs by {comp['block_diff']}")
            print()


def print_inventory_check(data: dict):
    """Print inventory_check results in human-readable format."""
    print()
    print("Flow Inventory Across Instances")
    print("─" * 100)

    # Group by flow name for display
    flows_by_name = {}
    for result in data["results"]:
        if result["flow_name"] not in flows_by_name:
            flows_by_name[result["flow_name"]] = []
        flows_by_name[result["flow_name"]].append(result)

    print(f"{'Flow Name':<40} {'Status':<15} {'Hashes':<45}")
    print("─" * 100)

    for flow_name in sorted(flows_by_name.keys()):
        results = flows_by_name[flow_name]
        unique_hashes = len(set(r["hash"] for r in results))

        status = "✓ All Match" if unique_hashes == 1 else f"✗ {unique_hashes} version(s)"

        hash_str = ", ".join([f"{r['instance']}:{r['hash']}" for r in sorted(results, key=lambda x: x["instance"])])
        if len(hash_str) > 45:
            hash_str = hash_str[:42] + "..."

        print(f"{flow_name:<40} {status:<15} {hash_str:<45}")

    print()
    print(f"Total: {data['summary']['total_unique_flows']} unique flows")
    print()


def export_to_csv(data: dict) -> str:
    """Export data to CSV format."""
    output = StringIO()

    if data["mode"] in ("quick", "detail"):
        writer = csv.DictWriter(output, fieldnames=["flow_name", "label", "region", "hash", "status"])
        writer.writeheader()
        for result in data["results"]:
            writer.writerow({
                "flow_name": result["flow_name"],
                "label": result["label"],
                "region": result["region"],
                "hash": result["hash"],
                "status": result["status"],
            })
    elif data["mode"] == "inventory":
        writer = csv.DictWriter(output, fieldnames=["flow_name", "instance", "hash", "flow_id", "arn"])
        writer.writeheader()
        for result in data["results"]:
            writer.writerow({
                "flow_name": result["flow_name"],
                "instance": result["instance"],
                "hash": result["hash"],
                "flow_id": result["flow_id"],
                "arn": result["arn"],
            })

    return output.getvalue()


def export_to_json(data: dict) -> str:
    """Export data to JSON format."""
    return json.dumps(data, indent=2)


def main():
    p = argparse.ArgumentParser(
        description="Compare contact flows across regions and accounts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Quick hash comparison
  %(prog)s --flow "Main IVR" --instances UUID1:us-east-1:prod UUID2:eu-west-1:prod

  # With detailed block diffs
  %(prog)s --flow "Main IVR" --instances UUID1:us-east-1 UUID2:us-west-2 --detail

  # Inventory of all flows
  %(prog)s --inventory --instances UUID1:us-east-1 UUID2:eu-west-1
        """,
    )

    p.add_argument("--flow", help="Flow name to check (exact match, case-insensitive)")
    p.add_argument(
        "--instances",
        nargs="+",
        required=True,
        metavar="UUID:REGION[:LABEL]",
        help="Instance specs (e.g., 'abc-123:us-east-1:prod')",
    )
    p.add_argument("--profile", help="AWS named profile")
    p.add_argument("--detail", action="store_true", help="Show detailed block diffs for mismatches")
    p.add_argument("--inventory", action="store_true", help="Show all flows across instances")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("--csv", action="store_true", help="Output as CSV")
    p.add_argument("--output", help="Write output to file (default: stdout)")

    args = p.parse_args()

    # Parse instances
    try:
        instances = [parse_instance_spec(spec) for spec in args.instances]
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Run appropriate check
    if args.inventory:
        data = inventory_check(instances, args.profile)
    elif args.flow:
        if args.detail:
            data = detail_check(instances, args.flow, args.profile)
        else:
            data = quick_check(instances, args.flow, args.profile)
    else:
        p.print_help()
        sys.exit(1)

    # Format output
    if args.json:
        output = export_to_json(data)
    elif args.csv:
        output = export_to_csv(data)
    else:
        # Human-readable output
        if data["mode"] == "quick":
            print_quick_check(data)
            return
        elif data["mode"] == "detail":
            print_detail_check(data)
            return
        elif data["mode"] == "inventory":
            print_inventory_check(data)
            return

    # Write output to file or stdout
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Output written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
