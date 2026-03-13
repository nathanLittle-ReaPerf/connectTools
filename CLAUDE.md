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
