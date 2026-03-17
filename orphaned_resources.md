# orphaned_resources.py

Audit an Amazon Connect instance for unused resources. Scans all contact flow content to extract every resource reference, then cross-references against the full instance inventory. Useful for identifying dead weight before a cleanup or migration.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python orphaned_resources.py --instance-id <UUID> --region us-east-1
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--check-lambdas` | Call `GetFunction` on each referenced Lambda ARN to verify it still exists |
| `--json` | Print results as JSON to stdout |
| `--csv FILE` | Write orphaned rows to `~/.connecttools/orphaned_resources/<FILE>` |

### Examples

```bash
# Full audit
python orphaned_resources.py --instance-id <UUID> --region us-east-1

# Also verify Lambda ARNs exist
python orphaned_resources.py --instance-id <UUID> --check-lambdas

# JSON output
python orphaned_resources.py --instance-id <UUID> --json | jq '.orphaned_flows'

# Export to CSV
python orphaned_resources.py --instance-id <UUID> --csv orphans.csv
```

## Output

```
  ────────────────────────────────────────────────────────────────────────
  ORPHANED RESOURCES   dbff2776-6bba-4071-98dc-03c16bf2e6de
  ────────────────────────────────────────────────────────────────────────
  47 flow(s) scanned

  ── ORPHANED FLOWS — not called by any flow or phone number ─────────────
  Old Sales IVR          CONTACT_FLOW   abc12345-...
  Deprecated Whisper     AGENT_WHISPER  def67890-...

  ── ORPHANED QUEUES — exist in instance but not referenced in any flow ──
  Temp Test Queue
    arn:aws:connect:us-east-1:123456789012:instance/.../queue/...

  ── ORPHANED PROMPTS — exist in instance but not referenced in any flow ─
  holiday_greeting_2024
    arn:aws:connect:us-east-1:123456789012:instance/.../prompt/...

  ── ORPHANED HOURS OF OPERATION ─────────────────────────────────────────
  (none)

  ── LAMBDA REFERENCES — referenced in flows ─────────────────────────────
  arn:aws:lambda:us-east-1:123456789012:function:my-auth-fn
  arn:aws:lambda:us-east-1:123456789012:function:my-lookup-fn

  ────────────────────────────────────────────────────────────────────────
  4 orphaned resource(s) found
```

**CSV columns:** `category`, `name`, `id`, `type`, `arn`, `note`

**JSON keys:** `orphaned_flows`, `orphaned_queues`, `orphaned_prompts`, `orphaned_hours`, `lambda_arns`, `lambda_check` (when `--check-lambdas` used)

## What it checks

| Category | Definition |
|---|---|
| Orphaned flows | Not referenced by any `TransferContactToFlow` / `InvokeFlowModule` block in any flow, AND not assigned to any phone number via `ListPhoneNumbersV2` |
| Orphaned queues | Exist in the instance (`ListQueues`) but no `SetQueue` block in any flow references them |
| Orphaned prompts | Exist in the instance (`ListPrompts`) but no flow block references them |
| Orphaned hours | Exist in the instance (`ListHoursOfOperations`) but no `CheckHoursOfOperation` block references them |
| Lambda references | All unique Lambda ARNs used across all flows; use `--check-lambdas` to probe each one |

## Key Behaviours

- **Snapshot integration** — if a snapshot exists (`instance_snapshot.py`), resource lists (queues, prompts, hours) are loaded from it without extra API calls. Flow content is always fetched live since the snapshot doesn't store block definitions.
- **Dual ID/ARN matching** — resources are matched by both bare ID and full ARN to avoid false positives.
- **Phone numbers included** — flow–phone number assignments are fetched via `ListPhoneNumbersV2` and counted as entry-point references, so flows used as IVR entry points are not incorrectly flagged.
- **ARN scan fallback** — in addition to known field names, all flow Parameters are recursively scanned for Connect ARN patterns (`arn:aws:connect:...`) to catch any references the known-field lookups might miss.
- **`--check-lambdas`** — for each referenced Lambda ARN, calls `GetFunction` and flags any that return `ResourceNotFoundException`. Requires `lambda:GetFunction`.

## Required IAM Permissions

```
connect:ListContactFlows
connect:DescribeContactFlow
connect:ListQueues
connect:ListPrompts
connect:ListHoursOfOperations
connect:ListPhoneNumbersV2
lambda:GetFunction    (only with --check-lambdas)
```

## Changelog

| Version | Change |
|---|---|
| Initial | Orphaned flows, queues, prompts, hours; Lambda reference list; --check-lambdas; CSV/JSON output |
