# phone_numbers.py

List every claimed phone number on an Amazon Connect instance alongside the contact flow it routes to. Useful for auditing DID/toll-free assignments, finding unassigned numbers, and confirming routing before go-live.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python phone_numbers.py --instance-id <UUID> --region us-east-1
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--flow NAME` | Filter to numbers associated with a flow (case-insensitive substring) |
| `--unassigned` | Show only numbers with no contact flow assigned |
| `--csv FILE` | Write results to `~/.connecttools/phone_numbers/<FILE>` |
| `--json` | Print results as a JSON array to stdout |

### Examples

```bash
# All phone numbers with their flows
python phone_numbers.py --instance-id <UUID> --region us-east-1

# Numbers routed to a specific flow
python phone_numbers.py --instance-id <UUID> --flow "Main IVR"

# Find unassigned numbers
python phone_numbers.py --instance-id <UUID> --unassigned

# Export full list to CSV
python phone_numbers.py --instance-id <UUID> --csv phone_numbers.csv

# JSON — pipe to jq
python phone_numbers.py --instance-id <UUID> --json | jq '.[] | select(.flow == null)'
```

## Output

**Human-readable:**

```
  ────────────────────────────────────────────────────────────────────────
  PHONE NUMBERS   dbff2776-6bba-4071-98dc-03c16bf2e6de
  ────────────────────────────────────────────────────────────────────────
  12 number(s)  ·  1 unassigned  ·  8 legacy (flow not in API)

  NUMBER          TYPE        COUNTRY  FLOW
  ─────────────   ─────────   ───────  ────────────────────────────────
  +14165550001    DID         CA       Main IVR
  +14165550002    DID         CA       (legacy config)
  +18005550100    TOLL_FREE   US       Support IVR
  +14165559999    DID         CA       (unassigned)
```

**CSV columns:** `number`, `type`, `country`, `flow`, `status`, `phone_number_id`, `target_arn`

**JSON:** array of objects with the same fields.

## Key Behaviours

- **Snapshot-first resolution** — if an instance snapshot exists (from `instance_snapshot.py`), flow names are resolved offline with no extra API calls. Without a snapshot, one `DescribeContactFlow` call is made per unique flow ARN (results cached within the run).
- **Unassigned vs legacy** — `ListPhoneNumbersV2` `TargetArn` reflects the claiming entity, not the contact flow:
  - `null` / empty → truly unassigned, shown as `(unassigned)`
  - Instance or TDG ARN (no `/contact-flow/` in path) → number was configured via the old Connect console UI; the contact flow association is not exposed by the API → shown as `(legacy config)` in yellow
  - Contact-flow ARN → resolved to the flow name
- **`--unassigned` filter** — matches only numbers with no `TargetArn` (null). Legacy-configured numbers are excluded from this filter since they do have a flow — it's just not available via the API.
- **`--flow` filter** — excludes legacy numbers (only matches resolved flow names).
- **Status flagging** — numbers not in `CLAIMED` state (e.g. `IN_PROGRESS`, `FAILED`) are highlighted in yellow.
- **Sorted output** — numbers are sorted alphabetically by phone number string.

## Required IAM Permissions

```
connect:ListPhoneNumbersV2
connect:DescribeContactFlow   (only when no snapshot is available)
```

## APIs Used

| API | Purpose |
|---|---|
| `ListPhoneNumbersV2` | Fetch all claimed numbers with their `TargetArn` (associated flow) |
| `DescribeContactFlow` | Resolve flow ARN to name when no snapshot is present |

## Changelog

| Version | Change |
|---|---|
| Initial | List all phone numbers with flow names, filters, CSV/JSON output |
