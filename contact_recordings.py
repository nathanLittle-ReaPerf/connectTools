#!/usr/bin/env python3
"""contact-recordings: Find S3 locations and presigned URLs for a contact's recordings and transcripts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Locate S3 recordings and transcripts (redacted and non-redacted) for an Amazon Connect contact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --contact-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --contact-id <UUID> --url-expires 7200
  %(prog)s --instance-id <UUID> --contact-id <UUID> --json
        """,
    )
    p.add_argument("--instance-id",  required=True, metavar="UUID")
    p.add_argument("--contact-id",   required=True, metavar="UUID")
    p.add_argument("--region",       default=None,  help="AWS region (defaults to session/CloudShell region)")
    p.add_argument("--profile",      default=None,  help="AWS named profile")
    p.add_argument("--url-expires",  type=int, default=3600, metavar="SECONDS",
                   help="Presigned URL expiry in seconds (default: 3600)")
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Emit raw JSON (pipe-friendly)")
    return p.parse_args()


# ── Client factories ──────────────────────────────────────────────────────────

def make_clients(region, profile):
    session = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    connect = session.client("connect", region_name=resolved, config=RETRY_CONFIG)
    s3      = session.client("s3",      region_name=resolved, config=RETRY_CONFIG)
    return connect, s3


# ── Connect fetchers ──────────────────────────────────────────────────────────

def fetch_contact(connect, instance_id, contact_id):
    try:
        return connect.describe_contact(InstanceId=instance_id, ContactId=contact_id)["Contact"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        print(f"Error fetching contact [{code}]: {msg}", file=sys.stderr)
        sys.exit(1)


def fetch_storage_configs(connect, instance_id, resource_type):
    """Return list of S3Config dicts for the given resource type, or [] on any error."""
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


# ── S3 helpers ────────────────────────────────────────────────────────────────

def list_matching_objects(s3, bucket, prefix, contact_id):
    """Paginate ListObjectsV2 under prefix; return keys that contain contact_id."""
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


def presign(s3, bucket, key, expires):
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )
    except ClientError:
        return None


def classify_key(key):
    """Return 'redacted' or 'original' based on path and filename conventions."""
    if "/Redacted/" in key or "/redacted/" in key or "_redacted." in key.lower():
        return "redacted"
    return "original"


def search_prefixes(s3, bucket, prefixes, contact_id, expires):
    """
    Search multiple S3 prefixes, return list of result dicts.
    Deduplicates by key across prefixes.
    """
    seen, results = set(), []
    for prefix in prefixes:
        for key in list_matching_objects(s3, bucket, prefix, contact_id):
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "s3_uri":        f"s3://{bucket}/{key}",
                "presigned_url": presign(s3, bucket, key, expires),
                "subtype":       classify_key(key),
                "key":           key,
            })
    return results


# ── Artifact discovery ────────────────────────────────────────────────────────

def find_artifacts(connect, s3, instance_id, contact, expires):
    """
    Returns:
      {
        "recordings":  [...],   # VOICE only — original + redacted .wav files
        "analysis":    [...],   # Contact Lens JSON — VOICE and CHAT
        "transcripts": [...],   # CHAT only — Connect chat transcript files
      }
    Each item: {"s3_uri", "presigned_url", "subtype": "original"|"redacted", "key"}
    """
    channel    = contact.get("Channel", "VOICE")
    ts         = contact.get("InitiationTimestamp")
    contact_id = contact["Id"]

    if ts is None:
        print("Error: contact has no InitiationTimestamp.", file=sys.stderr)
        sys.exit(1)

    yyyy, mm, dd = ts.strftime("%Y"), ts.strftime("%m"), ts.strftime("%d")

    result: dict[str, list] = {"recordings": [], "analysis": [], "transcripts": []}

    # ── CALL_RECORDINGS bucket ────────────────────────────────────────────────
    for cfg in fetch_storage_configs(connect, instance_id, "CALL_RECORDINGS"):
        bucket = cfg["BucketName"]
        base   = cfg.get("BucketPrefix", "").rstrip("/")

        if channel == "VOICE":
            # Recordings live in CallRecordings/; redacted variant has _redacted in filename
            result["recordings"] += search_prefixes(
                s3, bucket,
                [
                    f"{base}/CallRecordings/{yyyy}/{mm}/{dd}/",
                    f"{base}/{yyyy}/{mm}/{dd}/",          # fallback if prefix includes subfolder
                ],
                contact_id, expires,
            )

            # Contact Lens analysis — non-redacted and redacted are in separate subdirs
            result["analysis"] += search_prefixes(
                s3, bucket,
                [
                    f"{base}/Analysis/Voice/{yyyy}/{mm}/{dd}/",
                    f"{base}/Analysis/Voice/Redacted/{yyyy}/{mm}/{dd}/",
                ],
                contact_id, expires,
            )

        elif channel == "CHAT":
            # Contact Lens for chat is stored in the CALL_RECORDINGS bucket
            result["analysis"] += search_prefixes(
                s3, bucket,
                [
                    f"{base}/Analysis/Chat/{yyyy}/{mm}/{dd}/",
                    f"{base}/Analysis/Chat/Redacted/{yyyy}/{mm}/{dd}/",
                ],
                contact_id, expires,
            )

    # ── CHAT_TRANSCRIPTS bucket ───────────────────────────────────────────────
    if channel == "CHAT":
        for cfg in fetch_storage_configs(connect, instance_id, "CHAT_TRANSCRIPTS"):
            bucket = cfg["BucketName"]
            base   = cfg.get("BucketPrefix", "").rstrip("/")

            # BucketPrefix typically already ends with ChatTranscripts
            result["transcripts"] += search_prefixes(
                s3, bucket,
                [
                    f"{base}/{yyyy}/{mm}/{dd}/",
                    f"{base}/Redacted/{yyyy}/{mm}/{dd}/",
                ],
                contact_id, expires,
            )

    return result


# ── Human-readable output ─────────────────────────────────────────────────────

def _section(title):
    print(f"\n{'─' * 64}")
    print(f"  {title}")
    print(f"{'─' * 64}")


def _row(label, value):
    print(f"  {label:<22} {value}")


def _print_group(items):
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


def print_human(contact, artifacts, expires):
    channel = contact.get("Channel", "?")
    ts      = contact.get("InitiationTimestamp")

    _section(f"RECORDINGS & TRANSCRIPTS   {contact.get('Id', '?')}")
    _row("Channel:",     channel)
    _row("Date:",        ts.strftime("%Y-%m-%d") if ts else "?")
    _row("URLs expire:", f"{expires}s ({expires // 60}m)")

    if channel == "VOICE":
        print("\n  RECORDINGS")
        _print_group(artifacts["recordings"])
        print("\n  CONTACT LENS ANALYSIS")
        _print_group(artifacts["analysis"])

    elif channel == "CHAT":
        print("\n  CHAT TRANSCRIPTS")
        _print_group(artifacts["transcripts"])
        print("\n  CONTACT LENS ANALYSIS")
        _print_group(artifacts["analysis"])

    else:
        for group_name, items in artifacts.items():
            print(f"\n  {group_name.upper()}")
            _print_group(items)

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    connect, s3 = make_clients(args.region, args.profile)

    contact = fetch_contact(connect, args.instance_id, args.contact_id)

    artifacts = find_artifacts(connect, s3, args.instance_id, contact, args.url_expires)

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)

        # Strip internal "key" field
        for group in artifacts.values():
            for item in group:
                item.pop("key", None)

        ts = contact.get("InitiationTimestamp")
        print(json.dumps(
            {
                "contact_id":          contact["Id"],
                "channel":             contact.get("Channel"),
                "date":                ts.strftime("%Y-%m-%d") if ts else None,
                "url_expires_seconds": args.url_expires,
                "artifacts":           artifacts,
            },
            indent=2,
            default=serial,
        ))
    else:
        print_human(contact, artifacts, args.url_expires)


if __name__ == "__main__":
    main()
