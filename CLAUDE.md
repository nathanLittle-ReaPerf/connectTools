# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A growing set of Python CLI tools for **Amazon Connect**. Each tool is a single self-contained script, designed to run in AWS CloudShell without any local setup beyond one pip install.

## Setup

Dependencies (`python-dateutil`, `openpyxl`) are auto-installed by `connectToolbox.py` on first run. For running individual scripts directly:
```bash
pip install python-dateutil openpyxl --user
```

## Tools

---

### `instance_snapshot.py` — Instance Snapshot

Fetches all listable resources from a Connect instance and stores them at `~/.connecttools/snapshot_<instance-id>.json`. Other tools load this snapshot to resolve IDs/ARNs to names without making live API calls.

```bash
# Fetch and save snapshot
python instance_snapshot.py --instance-id <UUID> --region us-east-1

# Show summary of stored snapshot (no API calls)
python instance_snapshot.py --instance-id <UUID> --show

# Search the snapshot by resource type and name fragment
python instance_snapshot.py --instance-id <UUID> --lookup queues "Billing"
python instance_snapshot.py --instance-id <UUID> --lookup flows "IVR"
python instance_snapshot.py --instance-id <UUID> --lookup users "jsmith"

# Dump full snapshot as JSON
python instance_snapshot.py --instance-id <UUID> --json | jq '.queues'
```

**APIs used:** `DescribeInstance`, `ListQueues`, `ListContactFlows`, `ListRoutingProfiles`, `ListHoursOfOperations`, `ListPrompts`, `ListQuickConnects`, `ListSecurityProfiles`, `ListPhoneNumbers`, `ListUsers`

**Required IAM:** `connect:DescribeInstance` + `connect:ListXxx` for each resource type above

**Helper module:** `ct_snapshot.py` — importable by other tools:
- `ct_snapshot.load(instance_id)` → snapshot dict or None
- `ct_snapshot.resolve(snapshot, resource_type, id_or_arn)` → name string or None
- `ct_snapshot.search(snapshot, resource_type, name_fragment)` → list of matching items
- `ct_snapshot.warn_if_stale(snapshot)` → prints stderr warning if >24h old
- `ct_snapshot.age_hours(snapshot)` → float

**Key behaviors:**
- Resources stored as dicts keyed by ID for O(1) lookup
- ARN resolution: extracts last path segment and looks up by ID
- Users stored with `username` as display name (full name requires `DescribeUser` — too expensive for bulk fetch)
- Missing/inaccessible resource types silently skipped (some may not be configured on all instances)
- `flow_scan.py` automatically loads the snapshot when `--instance-id` is provided to resolve broken reference IDs to human-readable names
- Stale threshold: 24h (configurable in `ct_snapshot.STALE_THRESHOLD`)

---

### `flow_scan.py` — Flow Error Scanner

Scan one or all contact flows for configuration issues. Works on local exported JSON files or live instance flows.

```bash
# Scan a local exported file
python flow_scan.py Main_IVR.json

# Scan a single flow by name from the instance
python flow_scan.py --instance-id <UUID> --name "Main IVR" --region us-east-1

# Scan all flows (summary table)
python flow_scan.py --instance-id <UUID> --all

# Bulk scan with per-block detail on flows that have issues
python flow_scan.py --instance-id <UUID> --all --detail

# Filter by flow type
python flow_scan.py --instance-id <UUID> --all --type CONTACT_FLOW

# JSON output (pipe to jq)
python flow_scan.py --instance-id <UUID> --all --json | jq '.flows[] | select(.issue_count > 0)'
```

**APIs used:** `ListContactFlows`, `DescribeContactFlow`

**Required IAM:** `connect:ListContactFlows`, `connect:DescribeContactFlow`

**Issues detected:**

| Severity | Kind | Description |
|---|---|---|
| ERROR | `broken_start` | StartAction references a block that doesn't exist |
| ERROR | `broken_target` | Transition (default/error/condition branch) points to a missing block |
| ERROR | `dead_end` | Non-terminal block with no outgoing transitions — contact gets stuck |
| ERROR | `missing_lambda_arn` | InvokeLambdaFunction block with empty ARN |
| WARN | `missing_error_branch` | Lambda/Transfer/InvokeFlow block with no error handler |
| WARN | `missing_default` | Decision block has conditions but no default (fallback) branch |
| WARN | `unreachable` | Block never referenced by any other block — dead code |
| WARN | `missing_queue` | SetQueue block with no queue configured |

**Key behaviors:**
- Accepts both the `export_flow.py` envelope format (`{"metadata":..., "content":...}`) and raw flow JSON
- `--all` bulk mode shows a summary table; add `--detail` for per-block breakdowns on flows with issues
- `--name` is case-insensitive substring match; exits if multiple flows match
- `--json` output includes `issue_count`, `errors`, `warnings`, and full `issues` array per flow

---

### `lambda_errors.py` — Lambda Error Aggregator

Scan Connect flow logs for a given Lambda function over a time window. Groups invocations by error type and lists affected contact IDs — useful for assessing blast radius after a bad Lambda deploy.

```bash
# Last 24h (default)
python lambda_errors.py --instance-id <UUID> --function my-auth-function --region us-east-1

# Custom window
python lambda_errors.py --instance-id <UUID> --function my-auth-function --last 4h
python lambda_errors.py --instance-id <UUID> --function my-auth-function --start 2026-03-15 --end 2026-03-16

# Export full contact list per error type
python lambda_errors.py --instance-id <UUID> --function my-auth-function --csv errors.csv

# Raw JSON (pipe to jq)
python lambda_errors.py --instance-id <UUID> --function my-auth-function --json | jq '.errors'
```

**APIs used:** `DescribeInstance`, `FilterLogEvents` (Connect flow log group)

**Required IAM:**
- `connect:DescribeInstance`
- `logs:FilterLogEvents` on `/aws/connect/<instance-alias>`

**Key behaviors:**
- `--function` is matched as a case-insensitive substring of the Lambda ARN in each log entry — can be a full ARN, function name, or partial name
- `--period` accepts `today`, `yesterday`, `this-week`, `last-week`, `this-month`, `last-month`
- `--last` accepts `30m`, `4h`, `7d`, etc.; `--start`/`--end` take `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`; default is `24h` if none specified
- Human output shows up to 15 contact IDs per error type; `--csv` / `--json` include all
- Errors are sorted by frequency (most common first)
- `--csv` columns: timestamp, contact_id, function_name, function_arn, flow_name, result, error_type

---

### `contact_timeline.py` — Contact Timeline

Chronological event timeline for a single contact. Stitches together contact metadata milestones (from DescribeContact), every flow block execution (from CloudWatch flow logs), Lambda invocations, and optionally Contact Lens transcript turns into a single sorted view.

```bash
# Human-readable timeline
python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Include Contact Lens transcript turns
python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --transcript

# Raw JSON (pipe to jq)
python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --json | jq '.events[] | select(.kind=="LAMBDA")'

# Save JSON to file
python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --output timeline.json
```

**APIs used:** `DescribeContact`, `DescribeInstance`, `DescribeQueue`, `DescribeUser`, `FilterLogEvents` (Connect flow log group), `ListRealtimeContactAnalysisSegmentsV2` (when `--transcript` or JSON output requested)

**Required IAM:**
- `connect:DescribeContact`
- `connect:DescribeInstance`
- `connect:DescribeQueue`
- `connect:DescribeUser`
- `logs:FilterLogEvents` on `/aws/connect/<instance-alias>`
- `connect:ListRealtimeContactAnalysisSegments` (for `--transcript`)

**Output columns:** `OFFSET` (T+MM:SS from contact start) · `KIND` · `EVENT` · `DETAIL`

**Event kinds:**
- `CONTACT` (bold) — metadata milestones: initiated, entered queue, agent connected, disconnected
- `FLOW` — flow block executions: Play prompt, Get input, Check attribute, Set queue, etc.
- `LAMBDA` (yellow) — Lambda invocations with result (Success/Error) and flow name
- `LENS` (dim) — Contact Lens transcript turns with speaker role and sentiment; shown with `--transcript`

**Key behaviors:**
- Timestamps from the flow log message's own `Timestamp` field (preferred) with CW event timestamp as fallback
- Lens transcript: uses `BeginOffsetMillis` for voice, `AbsoluteTime` for chat
- Log group auto-discovered from instance alias; override with `--log-group`
- Contact Lens only fetched when `--transcript`, `--json`, or `--output` is passed
- If no flow logs found, timeline shows contact milestones only with a warning

---

### `contact_diff.py` — Contact Diff

Side-by-side comparison of two contacts in the same instance. Diffs core metadata, custom attributes, and Contact Lens outcome to answer "why did these two contacts behave differently?"

```bash
# Human-readable diff
python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --region us-east-1

# Show all attributes (not just differing ones)
python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --all-attrs

# Raw JSON (pipe to jq)
python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --json | jq '.diff.attributes'
```

**APIs used:** `DescribeContact`, `GetContactAttributes`, `DescribeQueue`, `DescribeUser`, `ListRealtimeContactAnalysisSegmentsV2`

**Required IAM:**
- `connect:DescribeContact`
- `connect:GetContactAttributes`
- `connect:DescribeQueue`
- `connect:DescribeUser`
- `connect:ListRealtimeContactAnalysisSegments`

**Output sections:**
- **CORE** — always shows all rows: Channel, Initiation method, Queue, Agent, Duration, Initiated, Disconnected, Disconnect reason, Customer endpoint, Previous contact ID
- **ATTRIBUTES** — by default shows only differing keys (use `--all-attrs` for all); shows `(all match)` or `(none)` when appropriate
- **CONTACT LENS** — always shows all rows: Status, Turns, Agent sentiment, Customer sentiment, Categories, Issues, Post-contact summary

**Key behaviors:**
- Lens status is normalized to `"Expired (>24h)"` for both contacts to avoid spurious mismatches due to different reported ages
- Attribute keys missing from one side display as `[absent]` (dimmed)
- `--json` output includes full raw contact data plus a `diff` block with per-field match flags for all three sections
- Both contact IDs must belong to the same instance

---

### `contact_inspect.py` — Contact Deep Dive

Pull all available data for a single contact ID: core metadata, custom attributes, references, Contact Lens transcript/sentiment/issues, and transfer chain.

```bash
# Human-readable
python contact_inspect.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Include full transcript turns
python contact_inspect.py --instance-id <UUID> --contact-id <UUID> --transcript

# Raw JSON (pipe to jq)
python contact_inspect.py --instance-id <UUID> --contact-id <UUID> --json | jq '.contact.Channel'

# With a named AWS profile (local dev)
python contact_inspect.py --instance-id <UUID> --contact-id <UUID> --profile my-admin
```

**APIs used:** `DescribeContact`, `GetContactAttributes`, `ListContactReferences`, `ListRealtimeContactAnalysisSegments` (voice) / `ListRealtimeContactAnalysisSegmentsV2` (chat)

**Required IAM:**
- `connect:DescribeContact`
- `connect:GetContactAttributes`
- `connect:ListContactReferences`
- `connect:ListRealtimeContactAnalysisSegments`

**Key behaviors:**
- Contact Lens data has a 24-hour retention window — detects expired contacts and explains why rather than returning silently empty
- Walks `PreviousContactId` to reconstruct the full transfer chain automatically
- Channel-aware: uses the correct Contact Lens API for VOICE vs. CHAT/EMAIL
- All API failures degrade gracefully (missing sections are noted, not crashes)
- `--json` merges all API responses into a single document

---

### `lambda_tracer.py` — Trace Lambda Invocations

Trace every Lambda function invoked during a contact's flow execution. Pulls Connect flow-execution logs to find Lambda invocations, then fetches the actual Lambda CloudWatch logs around each invocation timestamp.

```bash
# Human-readable trace with full Lambda logs
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Show invocation metadata only (skip Lambda logs)
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --summary

# Raw JSON output
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --json

# Save to file
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --output trace.json
```

**APIs used:** `DescribeContact`, `DescribeInstance`, `FilterLogEvents` (Connect flow logs and Lambda log groups)

**Required IAM:**
- `connect:DescribeContact`
- `connect:DescribeInstance`
- `logs:FilterLogEvents` on Connect log group (`/aws/connect/<instance-alias>`)
- `logs:FilterLogEvents` on each `/aws/lambda/<function-name>` log group

**Key behaviors:**
- `--summary` mode displays invocation metadata (ARN, timestamp, duration, response) without fetching Lambda logs — useful for quick overview
- After `--summary` output, user can enter an invocation number to drill down and fetch full logs on demand
- Lambda logs are fetched within ±30 seconds of the Connect-reported invocation timestamp
- High-concurrency Lambda functions may have unrelated log lines in the ±30s window; all are shown
- Log group is auto-discovered from instance alias (case-sensitive); override with `--log-group` if needed
- All API failures degrade gracefully (missing sections are noted, not crashes)

---

### `routing_profile_audit.py` — Routing Profile Audit

Audit routing profiles: list queue assignments per profile (channel, priority, delay) and agent counts. Flag anomalies: profiles with no agents, profiles with no queues, and queues not assigned to any profile.

```bash
# All profiles with queue assignments and agent counts
python routing_profile_audit.py --instance-id <UUID> --region us-east-1

# Filter to one profile by name substring
python routing_profile_audit.py --instance-id <UUID> --name "Tier 2"

# Export to CSV
python routing_profile_audit.py --instance-id <UUID> --csv audit.csv

# Raw JSON
python routing_profile_audit.py --instance-id <UUID> --json | jq '.anomalies'
```

**APIs used:** `ListRoutingProfiles`, `ListRoutingProfileQueues`, `ListQueues`, `ListRoutingProfileUsers`, `ListUsers`, `DescribeUser`

**Required IAM:**
- `connect:ListRoutingProfiles`
- `connect:ListRoutingProfileQueues`
- `connect:ListQueues`
- `connect:ListRoutingProfileUsers`
- `connect:ListUsers`
- `connect:DescribeUser` (fallback for older boto3 lacking ListRoutingProfileUsers)

**Key behaviors:**
- Builds agent-count map: uses `ListRoutingProfileUsers` if available; falls back to `ListUsers` + `DescribeUser` per user for older boto3 versions
- Agent count fallback shows a progress bar (percentage-based) when describing users individually
- Flags three types of anomalies:
  - Profiles with no agents assigned
  - Profiles with no queues assigned
  - Queues not assigned to any routing profile
- `--name` filter is case-insensitive substring match
- CSV output includes: profile name, queue name, channel, priority, delay, agent count, and anomaly notes

---

### `export_flow.py` — Export a Contact Flow by Name

Export a contact flow's full JSON definition, identified by name. Useful for version-controlling flows, diffing changes, or migrating between instances.

```bash
# Export to <Flow Name>.json in the current directory
python export_flow.py --instance-id <UUID> --name "Main IVR" --region us-east-1

# Exact name match (default is case-insensitive substring)
python export_flow.py --instance-id <UUID> --name "Main IVR" --exact

# Write to a specific path
python export_flow.py --instance-id <UUID> --name "Main IVR" --output ./flows/main_ivr.json

# Print to stdout (pipe-friendly)
python export_flow.py --instance-id <UUID> --name "Main IVR" --stdout

# List all flows without exporting (great for discovery)
python export_flow.py --instance-id <UUID> --list
python export_flow.py --instance-id <UUID> --list --name "IVR"
python export_flow.py --instance-id <UUID> --list --type CONTACT_FLOW
```

**APIs used:** `ListContactFlows`, `DescribeContactFlow`

**Required IAM:** `connect:ListContactFlows`, `connect:DescribeContactFlow`

**Key behaviors:**
- Default name match is case-insensitive substring; use `--exact` for strict matching
- If multiple flows match, lists them and exits — never exports ambiguously
- Output is a JSON envelope: `{"metadata": {...}, "content": {...}}` — metadata makes exports self-describing without reading the flow body
- `--list` mode can be scoped by `--name` substring and/or `--type` to browse large instances
- Valid `--type` values: `CONTACT_FLOW`, `CUSTOMER_QUEUE`, `CUSTOMER_HOLD`, `CUSTOMER_WHISPER`, `AGENT_HOLD`, `AGENT_WHISPER`, `OUTBOUND_WHISPER`, `AGENT_TRANSFER`, `QUEUE_TRANSFER`, `CAMPAIGN`

**Migration note:** When importing a flow into a different instance, every ARN in the `content` block (queues, prompts, other flows) must be remapped to the target instance's ARNs.

---

### `flow_to_chart.py` — Contact Flow Visualizer

Convert an exported flow JSON into a flowchart. Accepts files from `export_flow.py` or raw flow content JSON.

```bash
# Mermaid (default) — paste into mermaid.live or GitHub
python flow_to_chart.py Main_IVR.json

# Self-contained HTML — open in any browser
python flow_to_chart.py Main_IVR.json --format html

# Graphviz DOT — render to image
python flow_to_chart.py Main_IVR.json --format dot
dot -Tpng Main_IVR.dot -o Main_IVR.png

# Full pipeline
python export_flow.py --instance-id <UUID> --name "Main IVR" --output Main_IVR.json
python flow_to_chart.py Main_IVR.json --format html
```

**No dependencies** beyond Python stdlib — no pip install required.

**Node shapes by action role:**
- Rectangle — standard actions (Play Message, Set Queue, Lambda, etc.)
- Diamond — branching actions (Check Attribute, Get Input, Check Hours)
- Oval — terminal actions (Disconnect, Transfer to Queue)

**Edge labels:** condition values (`= billing`), error types (`No Match`), unlabeled for default/success paths.

**Key behaviors:**
- Accepts both the `export_flow.py` envelope format (`{"metadata":..., "content":...}`) and raw flow JSON directly
- Action `Identifier` fields can be human-readable names (e.g. `"Main Menu"`) not just UUIDs — node IDs are sanitized to handle both
- HTML format uses **Cytoscape.js** (not Mermaid) for proper node sizing, text wrapping, and edge label placement
- HTML includes a **Colors panel** (click *Colors* button) with 4 preset themes and per-node-type color pickers with auto text contrast

---

### `contacts_handled.py` — Monthly Contacts Handled Total

Sum the CONTACTS_HANDLED metric across an entire instance for the previous calendar month.

```bash
# CloudShell (uses console session credentials automatically)
python contacts_handled.py --instance-id <UUID> --region <region>

# Local with a named profile
python contacts_handled.py --instance-id <UUID> --region us-east-1 --profile my-admin

# Using an instance ARN instead of ID
python contacts_handled.py --instance-arn <ARN> --region us-east-1
```

**Required IAM:** `connect:ListQueues`, `connect:GetMetricDataV2`, `sts:GetCallerIdentity`

**Key behaviors:**
- Automatically discovers all queue IDs via paginated `ListQueues`
- Batches queues in chunks of ≤100 to respect API filter limits
- Time window is always the previous full calendar month in UTC (override with `--timezone`)
- Accepts either `--instance-id` or `--instance-arn` (mutually exclusive)
- botocore retry config with exponential backoff (max 10 attempts)

---

### `contact_recordings.py` — Contact Recordings & Transcripts

Locate the S3 paths and generate presigned download URLs for a contact's recordings and transcripts — original and redacted — for both voice and chat.

```bash
# Human-readable
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Extend presigned URL expiry to 2 hours
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --url-expires 7200

# Raw JSON (pipe to jq)
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --json | jq '.artifacts'
```

**APIs used:** `DescribeContact`, `ListInstanceStorageConfigs`, `s3:ListObjectsV2`, `s3:GeneratePresignedUrl`

**Required IAM:**
- `connect:DescribeContact`
- `connect:ListInstanceStorageConfigs`
- `s3:ListBucket` on the recordings/transcripts bucket(s)
- `s3:GetObject` on the recordings/transcripts bucket(s)

**What it finds (VOICE):** recording (original + redacted), Contact Lens analysis (original + redacted)

**What it finds (CHAT):** chat transcript (original + redacted), Contact Lens analysis (original + redacted)

**Key behaviors:**
- Reads `ListInstanceStorageConfigs` for `CALL_RECORDINGS` and `CHAT_TRANSCRIPTS` — adapts to your instance's bucket names and prefixes automatically; no hardcoded bucket names
- Searches S3 under the contact's date prefix (`YYYY/MM/DD`) and filters by contact ID in the key name
- Classifies files as original vs. redacted by checking for `_redacted` in the filename or `/Redacted/` in the path
- Presigned URLs default to 1-hour expiry; override with `--url-expires <seconds>`
- `--json` output groups all results under `artifacts.recordings`, `artifacts.analysis`, and `artifacts.transcripts`

## Architecture

All scripts follow the same conventions:
- `Config(retries={"max_attempts": N, "mode": "adaptive"})` on every boto3 client
- `boto3.Session(profile_name=profile)` to support optional `--profile`
- Pagination handled inline in each fetcher function
- `--json` output uses a `default=serial` handler that converts datetimes to ISO strings

## CloudShell & Dependencies

- **boto3 auto-upgrade:** `connectToolbox.py` checks boto3 version on startup. If < 1.35.0, it auto-upgrades via pip and restarts via `os.execv`. This is required for `ListRoutingProfileUsers` in `routing_profile_audit.py`. Graceful fallback: if `ListRoutingProfileUsers` is unavailable (older boto3), the tool falls back to `ListUsers` + `DescribeUser` per user, with a percentage-based progress bar.
- **Python 3.8:** `str | None` union syntax not supported at runtime. Always add `from __future__ import annotations` at the top of every new tool.
