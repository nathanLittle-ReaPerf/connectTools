# contact_investigator.py

Unified contact investigation tool. Consolidates `contact_inspect`, `contact_timeline`,
`lambda_tracer`, `contact_recordings`, and `contact_logs` into a single script. Shared
API calls (DescribeContact, CloudWatch log fetch, Contact Lens) are made once and reused
across sections — running `--all` is not slower than running each tool individually.

## Dependencies

No pip install required beyond boto3 (pre-installed in AWS CloudShell).

## Usage

```bash
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> [SECTIONS] [options]
```

### Sections (default: `--overview --timeline`)

| Flag | Description |
|---|---|
| `--overview` | Contact metadata, custom attributes, references, transfer chain, Lens summary |
| `--timeline` | Chronological event timeline: flow blocks, Lambda calls, contact milestones |
| `--lambda` | Lambda invocation trace: ARN, result, Connect-side response |
| `--recordings` | S3 paths + presigned download URLs (original and redacted) |
| `--logs` | Download raw CloudWatch flow-execution logs to a JSON file |
| `--all` | Run all five sections |

### Options

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--contact-id` | Contact UUID (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--log-group` | Override auto-discovered Connect log group (default: `/aws/connect/<alias>`) |
| `--transcript` | Include Contact Lens transcript turns (overview + timeline sections) |
| `--lambda-logs` | Also fetch Lambda CloudWatch logs — ±30s window per invocation (slow) |
| `--url-expires` | Presigned URL expiry in seconds for `--recordings` (default: 3600) |
| `--json` | Emit all sections as a single JSON document |
| `--output FILE` | Write JSON output to a file |

## Examples

```bash
# Default: overview + timeline
python contact_investigator.py --instance-id <UUID> --contact-id <UUID>

# Full investigation with transcript
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --all --transcript

# Lambda trace + CloudWatch logs
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --lambda --lambda-logs

# Recordings only, 2-hour URLs
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --recordings --url-expires 7200

# JSON of all sections, pipe to jq
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --all --json \
  | jq '.overview.contact.Channel'

# Save full investigation to file
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --all --output investigation.json
```

## Output sections

### `--overview`
Equivalent to `contact_inspect.py`. Shows:
- Core metadata: channel, initiation method, timestamps, duration
- Queue and agent info with resolved names
- Transfer chain (walks `PreviousContactId`)
- Custom contact attributes
- ListContactReferences output
- Contact Lens summary: sentiment, issues, categories, post-contact summary

### `--timeline`
Equivalent to `contact_timeline.py`. Shows a sorted table of:
- **CONTACT** (bold) — metadata milestones: initiated, entered queue, agent connected, disconnected
- **FLOW** — every flow block executed: Play prompt, Get input, Check attribute, etc.
- **LAMBDA** (yellow) — Lambda invocations with Connect-side result
- **LENS** (dim) — transcript turns (only with `--transcript`)

Each row shows `T+MM:SS` offset from contact initiation.

### `--lambda`
Equivalent to `lambda_tracer.py --summary`. For each invocation:
- Function ARN and name
- Invocation timestamp and flow name
- Connect-side result (Success/Error) and response payload

Add `--lambda-logs` to also fetch each function's CloudWatch logs in a ±30-second window.

### `--recordings`
Equivalent to `contact_recordings.py`. For VOICE: recording files (original + redacted)
and Contact Lens analysis. For CHAT: chat transcript files and Contact Lens analysis.
Reads `ListInstanceStorageConfigs` — no hardcoded bucket names.

### `--logs`
Equivalent to `contact_logs.py`. Downloads raw CloudWatch flow-execution log events to
`~/.connecttools/ContactInvestigator/<contact-id>_logs.json`. In `--json` mode, raw
events are included inline in the JSON output instead.

## JSON output structure

```json
{
  "overview":    { "contact": {...}, "attributes": {...}, "transfer_chain": [...], "contact_lens": {...} },
  "timeline":    { "event_count": 42, "events": [{...}] },
  "lambda":      { "invocation_count": 2, "invocations": [{...}] },
  "recordings":  { "artifacts": { "recordings": [...], "analysis": [...] } },
  "logs":        { "event_count": 87, "events": [{...}] }
}
```

Only keys for sections that were run are present.

## IAM permissions

| Permission | Required by |
|---|---|
| `connect:DescribeContact` | All sections |
| `connect:GetContactAttributes` | `--overview` |
| `connect:ListContactReferences` | `--overview` |
| `connect:DescribeQueue` | `--overview`, `--timeline` |
| `connect:DescribeUser` | `--overview`, `--timeline` |
| `connect:ListRealtimeContactAnalysisSegmentsV2` | `--overview`, `--timeline` with `--transcript` |
| `connect:DescribeInstance` | `--timeline`, `--lambda`, `--logs` |
| `logs:FilterLogEvents` on `/aws/connect/*` | `--timeline`, `--lambda`, `--logs` |
| `logs:FilterLogEvents` on `/aws/lambda/*` | `--lambda` with `--lambda-logs` |
| `connect:ListInstanceStorageConfigs` | `--recordings` |
| `s3:ListBucket`, `s3:GetObject` | `--recordings` |
