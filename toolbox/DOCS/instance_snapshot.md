# instance_snapshot.py

Fetch and store a full inventory of an Amazon Connect instance — queues, contact flows, routing profiles, hours of operation, prompts, quick connects, security profiles, phone numbers, and users — and save it to `~/.connecttools/snapshot_<instance-id>.json`. This snapshot is used by other tools (such as `flow_scan.py`) as a fast, offline name-resolution cache so they can display human-readable names instead of raw UUIDs. The tool also supports viewing a stored snapshot summary, searching the snapshot by resource type and name, and dumping the full snapshot as JSON.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python instance_snapshot.py --instance-id <UUID> --region us-east-1
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--show` | Print a summary of the stored snapshot (resource counts, last refreshed) — no API calls; mutually exclusive with `--json` and `--lookup` |
| `--json` | Dump the full stored snapshot as JSON to stdout — no refresh; mutually exclusive with `--show` and `--lookup` |
| `--lookup TYPE NAME` | Search the snapshot for a resource by type and name fragment — mutually exclusive with `--show` and `--json` |

### Examples

```bash
# Fetch and save snapshot
python instance_snapshot.py --instance-id <UUID> --region us-east-1

# Show summary of stored snapshot (no API calls)
python instance_snapshot.py --instance-id <UUID> --show

# Search for a queue by name fragment
python instance_snapshot.py --instance-id <UUID> --lookup queues "Billing"

# Search for a flow
python instance_snapshot.py --instance-id <UUID> --lookup flows "IVR"

# Search for a user
python instance_snapshot.py --instance-id <UUID> --lookup users "jsmith"

# Dump full snapshot as JSON
python instance_snapshot.py --instance-id <UUID> --json
```

## Output

**Fetch mode (default):** Prints each resource category as it is fetched (to stderr), then displays the same summary as `--show`.

**`--show` summary:**

```
  ────────────────────────────────────────────────────────────
  INSTANCE SNAPSHOT   my-instance
  ────────────────────────────────────────────────────────────
  Instance ID  : xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  Fetched      : 2026-03-16 10:00:00 UTC  (2h ago)
  Stored at    : /home/user/.connecttools/snapshot_<id>.json

  Queues                      42
  Contact flows               87
  Routing profiles            12
  Hours of operation           5
  Prompts                     18
  Quick connects               7
  Security profiles            4
  Phone numbers               10
  Users                      150
```

**`--lookup`:** Lists matching resources with their name, ID, ARN, and type (where applicable).

**`--json`:** Full snapshot JSON including `instance_id`, `instance_alias`, `fetched_at`, and one key per resource type containing a dict keyed by resource ID.

## Key Behaviours

- The snapshot is stored at `~/.connecttools/snapshot_<instance-id>.json`. The directory is created if it does not exist.
- Other tools that use the snapshot warn if it is older than 24 hours. Run this tool again to refresh.
- Each resource type is fetched independently with graceful degradation — if a fetch fails (e.g. due to IAM permissions), that resource type is stored as an empty dict and a warning is printed; the rest of the snapshot is still saved.
- `--show`, `--json`, and `--lookup` are read-only modes that make no API calls. They exit with an error if no snapshot has been saved yet for the given instance ID.
- Valid `--lookup TYPE` values: `queues`, `flows`, `routing_profiles`, `hours_of_operation`, `prompts`, `quick_connects`, `security_profiles`, `phone_numbers`, `users`. Name matching is case-insensitive substring.
- Phone numbers use the phone number itself (e.g. `+15551234567`) as the display name.
- User entries store the username as the name; full first/last name resolution requires `DescribeUser` per user and is not performed during snapshot — use `agent_list.py` for full user detail.

## Required IAM Permissions

```
connect:DescribeInstance
connect:ListQueues
connect:ListContactFlows
connect:ListRoutingProfiles
connect:ListHoursOfOperations
connect:ListPrompts
connect:ListQuickConnects
connect:ListSecurityProfiles
connect:ListPhoneNumbers
connect:ListUsers
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeInstance` | Resolve instance alias for snapshot metadata |
| `ListQueues` | Fetch all queue summaries |
| `ListContactFlows` | Fetch all contact flow summaries |
| `ListRoutingProfiles` | Fetch all routing profile summaries |
| `ListHoursOfOperations` | Fetch all hours of operation summaries |
| `ListPrompts` | Fetch all prompt summaries |
| `ListQuickConnects` | Fetch all quick connect summaries |
| `ListSecurityProfiles` | Fetch all security profile summaries |
| `ListPhoneNumbers` | Fetch all phone number summaries |
| `ListUsers` | Fetch all user summaries |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: full instance inventory fetch, snapshot save/load, --show summary, --lookup search, --json dump, graceful per-resource degradation |
