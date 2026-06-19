#!/usr/bin/env python3
"""flow_check.py — Compare contact flows across regions and accounts.

Verify flow parity: quick hash comparison, detailed diffs, or full inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
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


def quick_check(instances: list[Instance], flow_name: str, profile: str | None = None):
    """Quick hash comparison of a flow across instances."""
    print()
    print(f"Flow: {flow_name}")
    print("─" * 80)

    flows_by_hash = {}
    results = []

    for inst in instances:
        client = make_client(inst.instance_id, inst.region, profile)
        flow = fetch_flow(client, inst.instance_id, flow_name)

        if not flow:
            results.append({
                "label": inst.label,
                "region": inst.region,
                "hash": "NOT FOUND",
                "status": "✗ MISSING",
            })
            continue

        content_hash = hash_flow_content(flow["content"])
        results.append({
            "label": inst.label,
            "region": inst.region,
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
            result["status"] = "✓ All Match"
        elif matching_count > 1:
            result["status"] = "✓ Match"
        else:
            result["status"] = "✗ DIFF"

    # Print table
    print(f"{'Label':<20} {'Region':<15} {'Hash':<20} {'Status':<15}")
    print("─" * 80)
    for result in results:
        print(f"{result['label']:<20} {result['region']:<15} {result['hash']:<20} {result['status']:<15}")
    print()

    # Summary
    unique_hashes = len(flows_by_hash)
    if unique_hashes == 1:
        print("✓ All flows are identical!")
    else:
        print(f"✗ Found {unique_hashes} different version(s)")
    print()


def detail_check(instances: list[Instance], flow_name: str, profile: str | None = None):
    """Detailed comparison with block diffs."""
    quick_check(instances, flow_name, profile)

    # Fetch all flows
    flows = {}
    for inst in instances:
        client = make_client(inst.instance_id, inst.region, profile)
        flow = fetch_flow(client, inst.instance_id, flow_name)
        if flow:
            flows[inst.label] = flow

    if len(flows) < 2:
        print("Need at least 2 flows to compare")
        return

    # Compare pairs
    labels = list(flows.keys())
    for i in range(len(labels) - 1):
        for j in range(i + 1, len(labels)):
            label_a, label_b = labels[i], labels[j]
            flow_a, flow_b = flows[label_a], flows[label_b]

            # Parse content
            content_a = flow_a["content"] if isinstance(flow_a["content"], dict) else json.loads(flow_a["content"])
            content_b = flow_b["content"] if isinstance(flow_b["content"], dict) else json.loads(flow_b["content"])

            # Use flow_compare to get diffs
            if content_a == content_b:
                print(f"✓ {label_a} ↔ {label_b}: Identical")
            else:
                print(f"\n✗ {label_a} ↔ {label_b}: DIFFERENCES")
                print("─" * 60)
                # Show brief diff summary
                blocks_a = content_a.get("Blocks", [])
                blocks_b = content_b.get("Blocks", [])
                print(f"  {label_a}: {len(blocks_a)} blocks")
                print(f"  {label_b}: {len(blocks_b)} blocks")
                if len(blocks_a) != len(blocks_b):
                    print(f"  ⚠ Block count differs by {abs(len(blocks_a) - len(blocks_b))}")
                print()


def inventory_check(instances: list[Instance], profile: str | None = None):
    """Show all flows across instances."""
    print()
    print("Flow Inventory Across Instances")
    print("─" * 100)

    all_flows = {}

    for inst in instances:
        client = make_client(inst.instance_id, inst.region, profile)
        flows = fetch_all_flows(client, inst.instance_id)

        for flow in flows:
            if flow.name not in all_flows:
                all_flows[flow.name] = {}
            all_flows[flow.name][inst.label] = flow.content_hash

    # Print table
    print(f"{'Flow Name':<40} {'Status':<15} {'Hashes':<45}")
    print("─" * 100)

    for flow_name in sorted(all_flows.keys()):
        hashes = all_flows[flow_name]
        unique_hashes = len(set(hashes.values()))

        if unique_hashes == 1:
            status = "✓ All Match"
        else:
            status = f"✗ {unique_hashes} version(s)"

        hash_str = ", ".join([f"{label}:{h}" for label, h in sorted(hashes.items())])
        if len(hash_str) > 45:
            hash_str = hash_str[:42] + "..."

        print(f"{flow_name:<40} {status:<15} {hash_str:<45}")

    print()
    print(f"Total: {len(all_flows)} unique flows")
    print()


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

    args = p.parse_args()

    # Parse instances
    try:
        instances = [parse_instance_spec(spec) for spec in args.instances]
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Run appropriate check
    if args.inventory:
        inventory_check(instances, args.profile)
    elif args.flow:
        if args.detail:
            detail_check(instances, args.flow, args.profile)
        else:
            quick_check(instances, args.flow, args.profile)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
