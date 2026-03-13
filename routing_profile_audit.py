#!/usr/bin/env python3
"""routing_profile_audit.py — Audit routing profiles in an Amazon Connect instance.

For each routing profile: lists assigned queues (channel, priority, delay) and
agent count. Flags anomalies: profiles with no agents, profiles with no queues,
and queues not assigned to any routing profile.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})


# ── Argument parsing ───────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Audit routing profiles in an Amazon Connect instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --name "Tier 2"
  %(prog)s --instance-id <UUID> --csv report.csv
  %(prog)s --instance-id <UUID> --json | jq '.anomalies'
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--region",      default=None,  help="AWS region")
    p.add_argument("--profile",     default=None,  help="Named AWS profile")
    p.add_argument("--name",        default=None,  metavar="SUBSTR",
                   help="Filter to routing profiles whose name contains SUBSTR (case-insensitive)")
    p.add_argument("--csv",         default=None,  metavar="FILE",
                   help="Write CSV output to FILE")
    p.add_argument("--json",        action="store_true", dest="output_json",
                   help="Print JSON to stdout")
    return p.parse_args()


# ── Client factory ─────────────────────────────────────────────────────────────

def make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


# ── Data fetchers ──────────────────────────────────────────────────────────────

def list_routing_profiles(client, instance_id):
    profiles, token = [], None
    while True:
        kwargs = {"InstanceId": instance_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_routing_profiles(**kwargs)
        except ClientError as e:
            _fatal("ListRoutingProfiles", e)
        profiles.extend(resp.get("RoutingProfileSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            break
    return sorted(profiles, key=lambda r: r["Name"].lower())


def list_routing_profile_queues(client, instance_id, rp_id):
    """Return list of {QueueId, QueueName(placeholder), Channel, Priority, Delay}."""
    entries, token = [], None
    while True:
        kwargs = {"InstanceId": instance_id, "RoutingProfileId": rp_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_routing_profile_queues(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            print(f"  Warning: could not fetch queues for profile [{code}]: {e.response['Error']['Message']}",
                  file=sys.stderr)
            return []
        for q in resp.get("RoutingProfileQueueConfigSummaryList", []):
            entries.append({
                "queue_id":   q["QueueId"],
                "queue_name": q.get("QueueName", q["QueueId"]),
                "channel":    q["Channel"],
                "priority":   q["Priority"],
                "delay":      q["Delay"],
            })
        token = resp.get("NextToken")
        if not token:
            break
    return sorted(entries, key=lambda e: (e["channel"], e["priority"], e["queue_name"].lower()))


def list_queues(client, instance_id):
    """Return {queue_id: queue_name} for all STANDARD queues."""
    queue_map, token = {}, None
    while True:
        kwargs = {"InstanceId": instance_id, "MaxResults": 100, "QueueTypes": ["STANDARD"]}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_queues(**kwargs)
        except ClientError as e:
            _fatal("ListQueues", e)
        for q in resp.get("QueueSummaryList", []):
            queue_map[q["Id"]] = q["Name"]
        token = resp.get("NextToken")
        if not token:
            break
    return queue_map


def build_agent_counts(client, instance_id, profile_ids):
    """Return {rp_id: agent_count} using ListRoutingProfileUsers per profile."""
    counts: dict[str, int] = {}
    for rp_id in profile_ids:
        count, token = 0, None
        while True:
            kwargs = {"InstanceId": instance_id, "RoutingProfileId": rp_id, "MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            try:
                resp = client.list_routing_profile_users(**kwargs)
            except ClientError as e:
                code = e.response["Error"]["Code"]
                print(f"  Warning: could not count agents for profile [{code}]: {e.response['Error']['Message']}",
                      file=sys.stderr)
                break
            count += len(resp.get("UserSummaryList", []))
            token = resp.get("NextToken")
            if not token:
                break
        counts[rp_id] = count
    return counts


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fatal(op, e):
    code = e.response["Error"]["Code"]
    msg  = e.response["Error"]["Message"]
    print(f"Error in {op} [{code}]: {msg}", file=sys.stderr)
    sys.exit(1)


def _hr(char="─", width=66):
    print("  " + char * width)


def _delay_str(seconds):
    return f"{seconds}s" if seconds else "0s"


# ── Analysis ───────────────────────────────────────────────────────────────────

def build_report(client, instance_id, name_filter):
    print("  Loading routing profiles...", file=sys.stderr)
    all_profiles = list_routing_profiles(client, instance_id)

    if name_filter:
        profiles = [p for p in all_profiles if name_filter.lower() in p["Name"].lower()]
        if not profiles:
            print(f"  No routing profiles matching '{name_filter}'.", file=sys.stderr)
            sys.exit(0)
    else:
        profiles = all_profiles

    print("  Loading queues...", file=sys.stderr)
    queue_map = list_queues(client, instance_id)  # {id: name}

    print("  Counting agents per profile...", file=sys.stderr)
    agent_counts = build_agent_counts(client, instance_id, [p["Id"] for p in profiles])

    print("  Loading queue assignments...", file=sys.stderr)
    profile_data = []
    queues_in_any_rp: set = set()
    for p in profiles:
        rp_id  = p["Id"]
        queues = list_routing_profile_queues(client, instance_id, rp_id)
        for q in queues:
            q["queue_name"] = queue_map.get(q["queue_id"], q["queue_name"])
            queues_in_any_rp.add(q["queue_id"])
        profile_data.append({
            "id":          rp_id,
            "name":        p["Name"],
            "arn":         p.get("Arn", ""),
            "agent_count": agent_counts.get(rp_id, 0),
            "queues":      queues,
        })

    # Anomalies
    anomalies = []
    for pd in profile_data:
        if pd["agent_count"] == 0:
            anomalies.append(f'Routing profile "{pd["name"]}" has 0 agents assigned')
        if not pd["queues"]:
            anomalies.append(f'Routing profile "{pd["name"]}" has no queues configured')
        else:
            channels = {q["channel"] for q in pd["queues"]}
            # Flag if a channel entry exists but all its queues have delay > 0
            # (not an error, just informational — skip)
            _ = channels  # reserved for future checks

    # Orphaned queues — only flag when not filtering by name (full audit)
    if not name_filter:
        for qid, qname in sorted(queue_map.items(), key=lambda x: x[1].lower()):
            if qid not in queues_in_any_rp:
                anomalies.append(f'Queue "{qname}" is not assigned to any routing profile')

    return {
        "profiles":       profile_data,
        "all_queue_map":  queue_map,
        "agent_counts":   agent_counts,
        "anomalies":      anomalies,
        "total_agents":   sum(pd["agent_count"] for pd in profile_data),
        "total_queues":   len(queue_map),
    }


# ── Output ─────────────────────────────────────────────────────────────────────

def print_human(report, instance_id):
    profiles = report["profiles"]
    _hr()
    print(f"  ROUTING PROFILE AUDIT   {instance_id}")
    _hr()
    print(
        f"\n  {len(profiles)} routing profile(s)  |  "
        f"{report['total_agents']} agent(s)  |  "
        f"{report['total_queues']} queue(s)\n"
    )

    for i, pd in enumerate(profiles, 1):
        print(f"  [{i}] {pd['name']}   ({pd['agent_count']} agent(s))")
        if pd["queues"]:
            # Column widths
            max_qname = max((len(q["queue_name"]) for q in pd["queues"]), default=5)
            col = max(max_qname, 5)
            print(f"       {'Channel':<7}  {'Queue':<{col}}  {'Priority':>8}  {'Delay':>6}")
            print(f"       {'───────':<7}  {'─' * col}  {'────────':>8}  {'─────':>6}")
            for q in pd["queues"]:
                print(
                    f"       {q['channel']:<7}  {q['queue_name']:<{col}}"
                    f"  {q['priority']:>8}  {_delay_str(q['delay']):>6}"
                )
        else:
            print(f"       (no queues configured)")
        print()

    if report["anomalies"]:
        _hr(char="─", width=66)
        print(f"  Anomalies ({len(report['anomalies'])})\n")
        for a in report["anomalies"]:
            print(f"  !  {a}")
        print()
    else:
        print("  No anomalies found.")
        print()

    _hr()
    print()


def write_csv(report, path):
    rows = []
    for pd in report["profiles"]:
        if pd["queues"]:
            for q in pd["queues"]:
                rows.append({
                    "RoutingProfile": pd["name"],
                    "RoutingProfileId": pd["id"],
                    "Agents": pd["agent_count"],
                    "Channel": q["channel"],
                    "Queue": q["queue_name"],
                    "QueueId": q["queue_id"],
                    "Priority": q["priority"],
                    "Delay": q["delay"],
                })
        else:
            rows.append({
                "RoutingProfile": pd["name"],
                "RoutingProfileId": pd["id"],
                "Agents": pd["agent_count"],
                "Channel": "",
                "Queue": "(no queues)",
                "QueueId": "",
                "Priority": "",
                "Delay": "",
            })
    fieldnames = ["RoutingProfile", "RoutingProfileId", "Agents",
                  "Channel", "Queue", "QueueId", "Priority", "Delay"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved → {path}", file=sys.stderr)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    args   = parse_args()
    client = make_client(args.region, args.profile)
    report = build_report(client, args.instance_id, args.name)

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)
        # Remove internal map from JSON output
        out = {k: v for k, v in report.items() if k != "all_queue_map"}
        print(json.dumps(out, indent=2, default=serial))
    elif args.csv:
        write_csv(report, args.csv)
    else:
        print_human(report, args.instance_id)


if __name__ == "__main__":
    main()
