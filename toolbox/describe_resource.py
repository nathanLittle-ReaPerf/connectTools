#!/usr/bin/env python3
"""describe_resource.py — Look up any Amazon Connect resource by ARN.

Given a full or partial Connect ARN, determines the resource type and calls the
appropriate Describe API to display its key properties.
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

_MAN = """\
NAME
    describe_resource.py — Look up any Amazon Connect resource by ARN

SYNOPSIS
    python describe_resource.py ARN [OPTIONS]

DESCRIPTION
    Parses a full or partial Connect ARN to determine the resource type, then
    calls the appropriate Describe API and displays key properties.

    Supported resource types: queue, routing-profile, contact-flow,
    contact-flow-module, user, hours-of-operation, phone-number,
    security-profile, quick-connect, prompt, agent-status.

    For a full ARN the instance ID and region are extracted automatically —
    --instance-id and --region are not required.  For a bare resource ID,
    pass --type to tell the tool what kind of resource it is, and supply
    --instance-id and --region as usual.

OPTIONS
    ARN
        Full Connect ARN, partial ARN fragment, or bare resource UUID.

    --instance-id UUID
        Amazon Connect instance UUID. Required when the ARN does not embed the
        instance ID (e.g. bare resource IDs). Ignored for phone-number.

    --region REGION
        AWS region. Extracted from a full ARN when present; falls back to the
        CloudShell/session region.

    --profile NAME
        Named AWS profile for local development.

    --type TYPE
        Resource type to look up. Required when ARN is a bare ID with no
        type prefix. One of: queue, routing-profile, contact-flow,
        contact-flow-module, user, hours-of-operation, phone-number,
        security-profile, quick-connect, prompt, agent-status.

    --json
        Print full Describe response as JSON to stdout.

    --man
        Print this manual page and exit.

EXAMPLES
    # Full ARN — no other args needed
    python describe_resource.py arn:aws:connect:us-east-1:123456789012:instance/UUID/queue/UUID

    # Partial path fragment
    python describe_resource.py instance/UUID/routing-profile/UUID --region us-east-1

    # Bare resource ID — type and instance required
    python describe_resource.py <queue-uuid> --type queue --instance-id <UUID> --region us-east-1

    # Phone number ARN (no instance needed)
    python describe_resource.py arn:aws:connect:us-east-1:123456789012:phone-number/UUID

    # JSON output
    python describe_resource.py arn:aws:connect:us-east-1:123456789012:instance/UUID/user/UUID --json

IAM PERMISSIONS
    connect:DescribeQueue
    connect:DescribeRoutingProfile
    connect:DescribeContactFlow
    connect:DescribeContactFlowModule
    connect:DescribeUser
    connect:DescribeHoursOfOperation
    connect:DescribePhoneNumber
    connect:DescribeSecurityProfile
    connect:DescribeQuickConnect
    connect:DescribePrompt
    connect:DescribeAgentStatus
    (only the permission for the resource type being looked up is required)
"""

# ── Resource handler table ─────────────────────────────────────────────────────
#
# method:       boto3 client method name (snake_case)
# id_param:     parameter name for the resource ID/ARN in that API call
# response_key: top-level key in the Describe response that holds the resource data
# label:        human-readable resource type name
# fields:       ordered list of fields to show in human output
# no_instance:  True = do not pass InstanceId (phone-number uses standalone Describe API)

HANDLERS: dict[str, dict] = {
    "queue": {
        "method":       "describe_queue",
        "id_param":     "QueueId",
        "response_key": "Queue",
        "label":        "Queue",
        "fields":       ["Name", "Description", "QueueType", "Status",
                         "HoursOfOperationId", "MaxContacts", "QueueArn"],
    },
    "routing-profile": {
        "method":       "describe_routing_profile",
        "id_param":     "RoutingProfileId",
        "response_key": "RoutingProfile",
        "label":        "Routing Profile",
        "fields":       ["Name", "Description", "DefaultOutboundQueueId",
                         "MediaConcurrencies", "RoutingProfileArn"],
    },
    "contact-flow": {
        "method":       "describe_contact_flow",
        "id_param":     "ContactFlowId",
        "response_key": "ContactFlow",
        "label":        "Contact Flow",
        "fields":       ["Name", "Type", "State", "Status", "Description", "Arn"],
    },
    "contact-flow-module": {
        "method":       "describe_contact_flow_module",
        "id_param":     "ContactFlowModuleId",
        "response_key": "ContactFlowModule",
        "label":        "Contact Flow Module",
        "fields":       ["Name", "Status", "Description", "Arn"],
    },
    "user": {
        "method":       "describe_user",
        "id_param":     "UserId",
        "response_key": "User",
        "label":        "User",
        "fields":       ["Username", "IdentityInfo", "RoutingProfileId",
                         "SecurityProfileIds", "HierarchyGroupId"],
    },
    "hours-of-operation": {
        "method":       "describe_hours_of_operation",
        "id_param":     "HoursOfOperationId",
        "response_key": "HoursOfOperation",
        "label":        "Hours of Operation",
        "fields":       ["Name", "Description", "TimeZone",
                         "Config", "HoursOfOperationArn"],
    },
    "phone-number": {
        "method":       "describe_phone_number",
        "id_param":     "PhoneNumberId",
        "response_key": "ClaimedPhoneNumberSummary",
        "label":        "Phone Number",
        "no_instance":  True,
        "fields":       ["PhoneNumber", "PhoneNumberType", "PhoneNumberCountryCode",
                         "PhoneNumberStatus", "TargetArn", "PhoneNumberArn"],
    },
    "security-profile": {
        "method":       "describe_security_profile",
        "id_param":     "SecurityProfileId",
        "response_key": "SecurityProfile",
        "label":        "Security Profile",
        "fields":       ["SecurityProfileName", "Description",
                         "OrganizationResourceId", "Arn"],
    },
    "quick-connect": {
        "method":       "describe_quick_connect",
        "id_param":     "QuickConnectId",
        "response_key": "QuickConnect",
        "label":        "Quick Connect",
        "fields":       ["Name", "Description", "QuickConnectType",
                         "QuickConnectConfig", "QuickConnectArn"],
    },
    "prompt": {
        "method":       "describe_prompt",
        "id_param":     "PromptId",
        "response_key": "Prompt",
        "label":        "Prompt",
        "fields":       ["Name", "PromptARN"],
    },
    "agent-status": {
        "method":       "describe_agent_status",
        "id_param":     "AgentStatusId",
        "response_key": "AgentStatus",
        "label":        "Agent Status",
        "fields":       ["Name", "Description", "Type", "State",
                         "DisplayOrder", "AgentStatusARN"],
    },
}

KNOWN_TYPES = sorted(HANDLERS)


# ── ARN parsing ────────────────────────────────────────────────────────────────

def parse_arn(s: str) -> dict:
    """Parse a Connect ARN (full or partial).

    Returns a dict with keys: region, account, instance_id, resource_type,
    resource_id, full_arn.  Any field that cannot be determined is None.
    """
    result: dict = {
        "region":        None,
        "account":       None,
        "instance_id":   None,
        "resource_type": None,
        "resource_id":   None,
        "full_arn":      None,
    }
    s = s.strip()

    # ── Full ARN ──────────────────────────────────────────────────────────────
    if s.startswith("arn:aws:connect:"):
        result["full_arn"] = s
        parts = s.split(":", 5)
        if len(parts) > 3 and parts[3]:
            result["region"] = parts[3]
        if len(parts) > 4 and parts[4]:
            result["account"] = parts[4]
        if len(parts) > 5:
            _parse_resource_path(parts[5], result)
        return result

    # ── Partial path starting with "instance/" ────────────────────────────────
    if s.startswith("instance/"):
        _parse_resource_path(s, result)
        return result

    # ── "type/id" pair ────────────────────────────────────────────────────────
    if "/" in s:
        idx   = s.index("/")
        rtype = s[:idx]
        rid   = s[idx + 1:]
        if rtype in HANDLERS:
            result["resource_type"] = rtype
            result["resource_id"]   = rid
        return result

    # ── Bare UUID / ID ────────────────────────────────────────────────────────
    result["resource_id"] = s
    return result


def _parse_resource_path(path: str, result: dict) -> None:
    """Populate result from a resource path like 'instance/IID/TYPE/RID'
    or a standalone type path like 'phone-number/RID'."""
    parts = path.split("/")

    if parts[0] == "instance":
        if len(parts) >= 2:
            result["instance_id"] = parts[1]
        if len(parts) >= 4:
            result["resource_type"] = parts[2]
            result["resource_id"]   = parts[3]
    elif parts[0] in HANDLERS:
        # e.g. phone-number/UUID
        result["resource_type"] = parts[0]
        if len(parts) >= 2:
            result["resource_id"] = parts[1]


# ── Client factory ─────────────────────────────────────────────────────────────

def make_client(region: str | None, profile: str | None):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


# ── Describe dispatcher ────────────────────────────────────────────────────────

def do_describe(
    client,
    parsed:               dict,
    resource_type_override: str | None,
    instance_id_override:   str | None,
) -> tuple[dict | None, str | None]:
    """Call the appropriate Describe API.  Returns (data, error_message)."""
    rtype = resource_type_override or parsed["resource_type"]
    iid   = instance_id_override   or parsed["instance_id"]
    rid   = parsed["resource_id"]
    farn  = parsed["full_arn"]

    if not rtype:
        known = ", ".join(KNOWN_TYPES)
        return None, (
            "Cannot determine resource type from input.\n"
            f"Pass --type TYPE.  Known types: {known}"
        )

    handler = HANDLERS.get(rtype)
    if not handler:
        known = ", ".join(KNOWN_TYPES)
        return None, f"Unknown resource type '{rtype}'.  Known types: {known}"

    if not handler.get("no_instance") and not iid:
        return None, (
            "Instance ID is required but was not found in the ARN.\n"
            "Pass --instance-id UUID, or provide a full Connect ARN."
        )

    # Prefer the full ARN as the resource identifier — most Describe APIs
    # accept it in place of the bare UUID.
    resource_ref = farn if farn else rid
    if not resource_ref:
        return None, "No resource ID or ARN found in the input."

    kwargs: dict = {handler["id_param"]: resource_ref}
    if not handler.get("no_instance"):
        kwargs["InstanceId"] = iid

    method = getattr(client, handler["method"])
    try:
        resp = method(**kwargs)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        return None, f"[{code}] {msg}"

    data = resp.get(handler["response_key"])
    if data is None:
        return None, (
            f"Unexpected response format — "
            f"'{handler['response_key']}' not found in API response."
        )
    return data, None


# ── Field formatting ───────────────────────────────────────────────────────────

def _format_field(key: str, value) -> str | list:
    """Format a single field value for human display.

    Returns a string for single-line values, or a list of strings for
    multi-line values (caller indents continuation lines).
    """
    if value is None:
        return "(not set)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value if value else "(empty)"

    # ── Known complex types ───────────────────────────────────────────────────

    if key == "IdentityInfo" and isinstance(value, dict):
        first = value.get("FirstName") or ""
        last  = value.get("LastName")  or ""
        email = value.get("Email")     or ""
        name  = f"{first} {last}".strip()
        return f"{name}  <{email}>" if email else (name or "(empty)")

    if key == "MediaConcurrencies" and isinstance(value, list):
        parts = [
            f"{m.get('Channel', '?')}: {m.get('Concurrency', '?')}"
            for m in value
        ]
        return ", ".join(parts) if parts else "(none)"

    if key == "QuickConnectConfig" and isinstance(value, dict):
        lines = json.dumps(value, indent=2).splitlines()
        return lines if len(lines) > 1 else (lines[0] if lines else "(empty)")

    if key == "Config" and isinstance(value, list):
        # HoursOfOperation schedule entries
        lines = []
        for entry in value:
            day = entry.get("Day", "?")
            st  = entry.get("StartTime", {})
            et  = entry.get("EndTime",   {})
            start = f"{st.get('Hours', 0):02d}:{st.get('Minutes', 0):02d}"
            end   = f"{et.get('Hours', 0):02d}:{et.get('Minutes', 0):02d}"
            lines.append(f"{day:<12} {start} – {end}")
        return lines if lines else ["(none)"]

    if key == "PhoneNumberStatus" and isinstance(value, dict):
        return value.get("Status") or str(value)

    # ── Generic containers ────────────────────────────────────────────────────

    if isinstance(value, list):
        if not value:
            return "(none)"
        if all(isinstance(x, str) for x in value):
            return ", ".join(value)
        lines = json.dumps(value, indent=2).splitlines()
        return lines if len(lines) > 1 else (lines[0] if lines else "(empty)")

    if isinstance(value, dict):
        if not value:
            return "(empty)"
        pairs = [f"{k}: {v}" for k, v in value.items() if v is not None]
        inline = "  ".join(pairs)
        if len(inline) <= 80:
            return inline
        lines = json.dumps(value, indent=2).splitlines()
        return lines if len(lines) > 1 else (lines[0] if lines else "(empty)")

    return str(value)


# ── Human output ───────────────────────────────────────────────────────────────

def _hr(width: int = 52) -> None:
    print("  " + "─" * width)


def print_human(rtype: str, data: dict, parsed: dict) -> None:
    handler = HANDLERS[rtype]
    label   = handler["label"]
    fields  = handler["fields"]

    col = max(len(f) for f in fields) + 2

    print()
    _hr()
    print(f"  {label.upper()}")
    _hr()
    print()

    for field in fields:
        value     = data.get(field)
        formatted = _format_field(field, value)

        if isinstance(formatted, list):
            print(f"  {field:<{col}}  {formatted[0]}")
            pad = " " * col
            for line in formatted[1:]:
                print(f"  {pad}  {line}")
        else:
            print(f"  {field:<{col}}  {formatted}")

    print()

    # Show provenance
    dim = "\033[90m"
    rst = "\033[0m"
    if parsed.get("full_arn"):
        print(f"  {dim}ARN      {parsed['full_arn']}{rst}")
    elif parsed.get("resource_id"):
        print(f"  {dim}ID       {parsed['resource_id']}{rst}")
    if parsed.get("instance_id"):
        print(f"  {dim}Instance {parsed['instance_id']}{rst}")
    if parsed.get("region"):
        print(f"  {dim}Region   {parsed['region']}{rst}")

    print()
    _hr()
    print()


# ── Argument parsing ───────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Look up any Amazon Connect resource by ARN.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
resource types:
  {', '.join(KNOWN_TYPES)}

examples:
  %(prog)s arn:aws:connect:us-east-1:123456789012:instance/UUID/queue/UUID
  %(prog)s instance/UUID/routing-profile/UUID --region us-east-1
  %(prog)s <queue-uuid> --type queue --instance-id <UUID> --region us-east-1
  %(prog)s arn:aws:connect:us-east-1:123456789012:phone-number/UUID
  %(prog)s <arn> --json
        """,
    )
    p.add_argument(
        "arn",
        metavar="ARN",
        help="Full Connect ARN, partial ARN fragment, or bare resource UUID.",
    )
    p.add_argument("--instance-id", default=None, metavar="UUID",
                   help="Instance UUID (required for bare IDs not embedded in a full ARN)")
    p.add_argument("--region",      default=None, help="AWS region")
    p.add_argument("--profile",     default=None, help="Named AWS profile")
    p.add_argument(
        "--type",
        dest="resource_type",
        default=None,
        metavar="TYPE",
        choices=KNOWN_TYPES,
        help=f"Resource type — required when ARN is a bare ID: {{{', '.join(KNOWN_TYPES)}}}",
    )
    p.add_argument("--json", action="store_true", dest="output_json",
                   help="Print JSON to stdout")
    return p.parse_args()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args   = parse_args()
    parsed = parse_arn(args.arn)

    # CLI args override ARN-extracted values
    region = args.region       or parsed["region"]
    iid    = args.instance_id  or parsed["instance_id"]
    rtype  = args.resource_type or parsed["resource_type"]

    client = make_client(region, args.profile)

    data, err = do_describe(client, parsed, rtype, iid)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    if args.output_json:
        def serial(o):
            return o.isoformat() if hasattr(o, "isoformat") else str(o)
        print(json.dumps(data, indent=2, default=serial))
    else:
        print_human(rtype, data, parsed)


if __name__ == "__main__":
    main()
