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
- `flow_analyze.py` automatically loads the snapshot when `--instance-id` is provided to resolve broken reference IDs to human-readable names
- Stale threshold: 24h (configurable in `ct_snapshot.STALE_THRESHOLD`)

---

### `flow_attr_search.py` — Flow Attribute Search

Search one or all contact flows for every place a contact attribute is set, checked, or referenced.

```bash
# Search a local exported file
python flow_attr_search.py --attribute myAttr Main_IVR.json

# Search a single flow by name from the instance
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --name "Main IVR" --region us-east-1

# Search all flows (summary table)
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --all

# Bulk search with per-block detail
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --all --detail

# Exact-case match
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --all --exact

# JSON output
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --all --json | jq '.flows[] | select(.hit_count > 0)'
```

**APIs used:** `ListContactFlows`, `DescribeContactFlow`

**Required IAM:** `connect:ListContactFlows`, `connect:DescribeContactFlow`

**Hit kinds:**

| Kind | Description |
|---|---|
| `SET` | Attribute key is assigned in an `UpdateContactAttributes` block |
| `CHECK` | Attribute is the subject of a `Compare` block |
| `REF` | `$.Attributes.<name>` appears anywhere else in block parameters |

**Key behaviors:**
- `--attribute` match is case-insensitive by default; `--exact` for strict case
- Attribute name matched as a whole token — searching `foo` will not match `fooBar`
- `--all` bulk mode shows a summary table; `--detail` for per-block breakdown on flows with hits
- Accepts both the `export_flow.py` envelope format and raw flow JSON
- `--json` output: `hit_count`, `set_count`, `check_count`, `ref_count`, full `hits` array per flow

---

### `flow_analyze.py` — Flow Analyzer

Scan and optimize contact flows in a single pass. Combines hard error detection (broken refs, dead ends, missing handlers) with rule-based best-practice suggestions (UX, reliability, structure, maintainability). Default runs both; use `--scan` or `--optimize` alone to restrict to one pass.

```bash
# Local file — scan + optimize (default)
python flow_analyze.py Main_IVR.json

# Single flow, scan only
python flow_analyze.py --instance-id <UUID> --name "Main IVR" --scan

# All flows, full analysis with per-block detail
python flow_analyze.py --instance-id <UUID> --all --detail

# Bulk JSON — flows with scan errors
python flow_analyze.py --instance-id <UUID> --all --json | jq '.flows[] | select(.scan.issue_count > 0)'
```

**APIs used:** `ListContactFlows`, `DescribeContactFlow`

**Required IAM:** `connect:ListContactFlows`, `connect:DescribeContactFlow`

**Scan findings (`--scan`):** `broken_start` · `broken_target` · `dead_end` · `missing_lambda_arn` (all ERROR); `missing_error_branch` · `missing_default` · `unreachable` · `missing_queue` (all WARN)

**Optimize suggestions (`--optimize`):** menu depth > 5, GetUserInput with no error handler, transfer without staffing check, no hours-of-operation check, flow > 40 blocks, back-to-back Lambda calls, duplicate prompt text in 3+ blocks

**Key behaviors:**
- Accepts `export_flow.py` envelope format and raw flow JSON
- `--all` shows summary table; add `--detail` for per-block breakdown on flows with findings
- `--json` output: `scan` and `optimize` keys per flow (only keys for passes that ran)
- `--csv` writes scan issues (one row per issue)

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

### `contact_investigator.py` — Contact Investigator

Unified contact investigation tool. Consolidates what were formerly `contact_inspect`, `contact_timeline`, `lambda_tracer`, `contact_recordings`, and `contact_logs` into one script. Shared API calls (DescribeContact, CloudWatch log fetch, Contact Lens) are made once and reused across sections.

```bash
# Default: overview + timeline
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Full investigation with transcript
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --all --transcript

# Lambda trace with CloudWatch logs
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --lambda --lambda-logs

# Recordings only, 2-hour URLs
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --recordings --url-expires 7200

# JSON of all sections
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --all --json | jq '.overview.contact.Channel'

# Download raw flow logs
python contact_investigator.py --instance-id <UUID> --contact-id <UUID> --logs
```

**Sections:** `--overview` · `--timeline` · `--lambda` · `--recordings` · `--logs` · `--all`
Default (no section flags): `--overview --timeline`

**Required IAM:**
- `connect:DescribeContact` (all sections)
- `connect:GetContactAttributes`, `connect:ListContactReferences` (`--overview`)
- `connect:DescribeQueue`, `connect:DescribeUser` (`--overview`, `--timeline`)
- `connect:ListRealtimeContactAnalysisSegmentsV2` (`--overview`, `--timeline` with `--transcript`)
- `connect:DescribeInstance`, `logs:FilterLogEvents` on `/aws/connect/*` (`--timeline`, `--lambda`, `--logs`)
- `logs:FilterLogEvents` on `/aws/lambda/*` (`--lambda` with `--lambda-logs`)
- `connect:ListInstanceStorageConfigs`, `s3:ListBucket`, `s3:GetObject` (`--recordings`)

**Key behaviors:**
- `DescribeContact` called once; CloudWatch log events fetched once — both shared across all sections
- Contact Lens fetched at most once and reused by both `--overview` and `--timeline`
- `--lambda` shows invocation metadata and Connect-side responses; add `--lambda-logs` to also fetch each function's CW logs (±30s window)
- `--recordings` reads `ListInstanceStorageConfigs` — no hardcoded bucket names
- `--logs` writes raw CW events to `~/.connecttools/ContactInvestigator/<contact-id>_logs.json`; in `--json` mode events are included inline
- Log group auto-discovered from instance alias; override with `--log-group`
- `--json` aggregates all requested sections into a single document: keys present only for sections that ran

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

### `security_profile_diff.py` — Security Profile Diff

Compare the permission sets of two security profiles. Shows permissions only in A, only in B, and a count of shared permissions. Use `--all` to list shared permissions too.

```bash
# Human-readable diff
python security_profile_diff.py --instance-id <UUID> --profile-a "Agent" --profile-b "Supervisor" --region us-east-1

# Show shared permissions too
python security_profile_diff.py --instance-id <UUID> --profile-a "Agent" --profile-b "Admin" --all

# Export to CSV
python security_profile_diff.py --instance-id <UUID> --profile-a "Tier 1" --profile-b "Tier 2" --csv diff.csv

# Raw JSON
python security_profile_diff.py --instance-id <UUID> --profile-a "Agent" --profile-b "Supervisor" --json | jq '.only_in_b'
```

**APIs used:** `ListSecurityProfiles`, `ListSecurityProfilePermissions`

**Required IAM:**
- `connect:ListSecurityProfiles`
- `connect:ListSecurityProfilePermissions`

**Key behaviors:**
- `--profile-a` / `--profile-b` are case-insensitive substring matches; exits with a list if 0 or >1 profiles match
- Exits clearly if both names resolve to the same profile
- Output: red `─` = only in A, green `+` = only in B, dim `=` = shared (shown only with `--all`)
- Reports "identical" if both profiles have the same permission set
- CSV columns: `Permission`, `InA`, `InB`, `Status` (`only_in_a` / `only_in_b` / `shared`)

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

## Architecture

All scripts follow the same conventions:
- `Config(retries={"max_attempts": N, "mode": "adaptive"})` on every boto3 client
- `boto3.Session(profile_name=profile)` to support optional `--profile`
- Pagination handled inline in each fetcher function
- `--json` output uses a `default=serial` handler that converts datetimes to ISO strings

## CloudShell & Dependencies

- **boto3 auto-upgrade:** `connectToolbox.py` checks boto3 version on startup. If < 1.35.0, it auto-upgrades via pip and restarts via `os.execv`. This is required for `ListRoutingProfileUsers` in `routing_profile_audit.py`. Graceful fallback: if `ListRoutingProfileUsers` is unavailable (older boto3), the tool falls back to `ListUsers` + `DescribeUser` per user, with a percentage-based progress bar.
- **Python 3.8:** `str | None` union syntax not supported at runtime. Always add `from __future__ import annotations` at the top of every new tool.
