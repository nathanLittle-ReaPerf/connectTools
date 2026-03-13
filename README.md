# connectTools

A set of Python CLI tools for **Amazon Connect**. Each tool is a single self-contained script designed to run in AWS CloudShell with minimal setup.

## Quick Start

```bash
# Clone or upload scripts, then launch the toolbox
python connectToolbox.py
```

The toolbox checks and auto-installs missing dependencies (`python-dateutil`, `openpyxl`) on first run. No manual pip install needed.

The toolbox is an interactive menu launcher — select a tool with arrow keys or a number key, fill in the prompts, and get results. No flags to memorize. Instance ID, region, and AWS profile are saved to `~/.connecttools/config.json` after first use so you don't re-enter them every time.

## Tools

| Script | What it does |
|---|---|
| [`contacts_handled.py`](#contacts_handledpy) | Sum CONTACTS_HANDLED across all queues for a given month |
| [`contact_inspect.py`](#contact_inspectpy) | Deep dive on a single contact — metadata, attributes, Lens transcript, transfer chain |
| [`contact_search.py`](#contact_searchpy) | Search contacts by time range and optional filters; export to CSV or JSON |
| [`contact_recordings.py`](#contact_recordingspy) | S3 locations and presigned URLs for a contact's recordings and transcripts |
| [`contact_logs.py`](#contact_logspy) | Download CloudWatch flow-execution logs for a contact ID |
| [`lambda_tracer.py`](#lambda_tracerpy) | Trace Lambda invocations and fetch execution logs for a contact |
| [`export_flow.py`](#export_flowpy) | Export a contact flow's JSON definition by name |
| [`flow_to_chart.py`](#flow_to_chartpy) | Convert an exported flow JSON to an interactive flowchart (HTML, Mermaid, or DOT) |
| [`log_insights.py`](#log_insightspy) | Run a CloudWatch Logs Insights query against Connect log groups; export to Excel |
| [`cid_journey.py`](#cid_journeypy) | Render a Cytoscape.js caller journey map from a CID Search Excel export |
| [`agent_list.py`](#agent_listpy) | List agents with routing profile, hierarchy, and security profile details |
| [`agent_activity.py`](#agent_activitypy) | Agent handle time and activity report by date range |
| [`connectToolbox.py`](#connecttoolboxpy) | Interactive menu launcher for all tools above |

---

## Setup

### AWS CloudShell (recommended)

1. Open CloudShell from the AWS Console (terminal icon, top right).
2. Clone this repo: `git clone <repo-url>`
3. Run the toolbox — dependencies are checked and installed automatically:

```bash
python connectToolbox.py
```

### Local Development (Windows — Cmder / Git Bash)

Install dependencies manually if not using the toolbox:

```bash
pip install boto3 python-dateutil openpyxl
```

Pass `--profile <name>` to any script to use a named AWS profile instead of the session credentials.

#### winpty requirement

The interactive toolbox (`connectToolbox`) requires **winpty** when run locally on Windows in Cmder or Git Bash. Without it, the menu displays but keyboard input is silently ignored — the prompt appears, you type, nothing happens.

**Why:** Python's `sys.stdin` in these terminals is a pipe, not a real TTY. `winpty` bridges the gap between the mintty PTY and the Windows console API that Python expects.

**Symptom — input is silently ignored:**
```
  Choice (press Enter to confirm):
```
The prompt appears and characters may echo, but pressing Enter does nothing. You may also see `sys.stdin.isatty()` return `False`.

**Fix:** Use the included wrapper script instead of calling Python directly:
```bash
./connectToolbox        # uses winpty automatically
```

`winpty` ships with **Git for Windows** — if you have Git Bash, you already have it. You can verify with `which winpty`.

---

## contacts_handled.py

Sum the `CONTACTS_HANDLED` metric across all queues for a given calendar month.

```bash
# Previous month (default)
python contacts_handled.py --instance-id <UUID> --region us-east-1

# Specific month
python contacts_handled.py --instance-id <UUID> --region us-east-1 --month 2025-12
```

**Required IAM:** `connect:ListQueues`, `connect:GetMetricDataV2`, `sts:GetCallerIdentity`

Data retention limit: ~3 months. Requests outside that window are detected and reported clearly.

---

## contact_inspect.py

Pull all available data for a single contact ID: core metadata, custom attributes, references, Contact Lens transcript/sentiment/issues, and transfer chain.

```bash
# Human-readable output
python contact_inspect.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Include full transcript
python contact_inspect.py --instance-id <UUID> --contact-id <UUID> --transcript

# Raw JSON (pipe to jq)
python contact_inspect.py --instance-id <UUID> --contact-id <UUID> --json | jq '.contact.Channel'
```

**Required IAM:** `connect:DescribeContact`, `connect:GetContactAttributes`, `connect:ListContactReferences`, `connect:ListRealtimeContactAnalysisSegmentsV2`, `connect:DescribeQueue`, `connect:DescribeUser`

Contact Lens data has a 24-hour retention window — the tool detects expired contacts and explains why rather than returning silently empty results.

---

## contact_search.py

Search contacts by time range with optional filters. Exports to CSV or JSON.

```bash
# All contacts for a date
python contact_search.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-02

# Voice inbound for a specific queue
python contact_search.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-02 \
    --channel VOICE --initiation-method INBOUND --queue <QUEUE-ID>

# Filter by custom contact attribute
python contact_search.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-02 \
    --attribute Department=Billing

# JSON output
python contact_search.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-02 --json | jq '.[0].Id'
```

**Required IAM:** `connect:SearchContacts`

`SearchContacts` is throttled at 0.5 req/s — the script sleeps between pages and prints live progress.

---

## lambda_tracer.py

Trace every Lambda function invoked during a contact. Pulls Connect flow-execution logs to find Lambda invocations, then fetches the actual Lambda CloudWatch logs around each invocation timestamp.

```bash
# Human-readable trace
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Save to JSON
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --output trace.json
```

**Required IAM:** `connect:DescribeContact`, `connect:DescribeInstance`, `logs:FilterLogEvents` (on both the Connect log group and each `/aws/lambda/<function-name>` log group)

Lambda logs are fetched within ±30 seconds of the Connect-reported invocation time. If a function has high concurrency, log lines from concurrent invocations may appear in the window.

---

## export_flow.py

Export a contact flow's full JSON definition, identified by name.

```bash
# Export to <Flow Name>.json
python export_flow.py --instance-id <UUID> --name "Main IVR" --region us-east-1

# List all flows (useful for discovery)
python export_flow.py --instance-id <UUID> --list

# Print to stdout
python export_flow.py --instance-id <UUID> --name "Main IVR" --stdout

# Exact name match
python export_flow.py --instance-id <UUID> --name "Main IVR" --exact
```

**Required IAM:** `connect:ListContactFlows`, `connect:DescribeContactFlow`

Output is a JSON envelope: `{"metadata": {...}, "content": {...}}`. If multiple flows match the name, the tool lists them and exits rather than exporting ambiguously.

---

## flow_to_chart.py

Convert an exported flow JSON into a flowchart.

```bash
# Interactive HTML viewer (recommended)
python flow_to_chart.py Main_IVR.json --format html

# Mermaid — paste into mermaid.live or GitHub
python flow_to_chart.py Main_IVR.json

# Graphviz DOT
python flow_to_chart.py Main_IVR.json --format dot
dot -Tpng Main_IVR.dot -o Main_IVR.png
```

No pip install required — pure Python stdlib.

The HTML output uses **Cytoscape.js** for proper node sizing, text wrapping, and edge label placement. Includes a **Colors panel** with preset themes and per-node-type color pickers.

Full pipeline:
```bash
python export_flow.py --instance-id <UUID> --name "Main IVR" --output Main_IVR.json
python flow_to_chart.py Main_IVR.json --format html
```

---

## log_insights.py

Run a CloudWatch Logs Insights query against Amazon Connect log groups and export results to Excel.

```bash
# Last 24 hours, auto-detect log group
python log_insights.py --query call_report.sql --last 24h

# Specific date range, named output
python log_insights.py --query call_report.sql --start 2026-03-01 --end 2026-03-02 --output report.xlsx

# List available Connect log groups
python log_insights.py --list-logs
```

**Required IAM:** `logs:DescribeLogGroups`, `logs:StartQuery`, `logs:GetQueryResults`

Write standard Logs Insights syntax in any `.sql` or `.txt` file and pass it with `--query`. If only one `/aws/connect/` log group exists it's used automatically; otherwise the tool prompts you to pick one.

Saved queries live in the `queries/` folder.

---

## cid_journey.py

Render an interactive Cytoscape.js caller journey map from a CID Search Excel export.

```bash
python cid_journey.py CID-<value>_2026-03-01.xlsx
```

Produces a self-contained HTML file showing each contact leg as a node, with edges representing the transfer chain. Useful for visualizing complex multi-transfer journeys.

---

## contact_recordings.py

Locate the S3 paths and generate presigned download URLs for a contact's recordings and transcripts — original and redacted — for both voice and chat.

```bash
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Extend presigned URL expiry to 2 hours
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --url-expires 7200

# Raw JSON
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --json
```

**Required IAM:** `connect:DescribeContact`, `connect:ListInstanceStorageConfigs`, `s3:ListBucket`, `s3:GetObject`

Presigned URLs default to 1-hour expiry. CloudShell IAM role credentials cap URLs at 1 hour regardless of the requested value — a note is printed if you request longer. Values above 7 days (604800s) are clamped automatically.

---

## contact_logs.py

Download CloudWatch flow-execution logs for a contact ID.

```bash
# JSON output (default)
python contact_logs.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Plain text
python contact_logs.py --instance-id <UUID> --contact-id <UUID> --text

# Override log group if auto-discovery gets the casing wrong
python contact_logs.py --instance-id <UUID> --contact-id <UUID> --log-group /aws/connect/myInstance
```

**Required IAM:** `connect:DescribeContact`, `connect:DescribeInstance`, `logs:FilterLogEvents`

The log group is auto-discovered from the instance alias. If the alias casing doesn't match the actual log group name, pass `--log-group` to override — the toolbox saves the correct name per instance so you only enter it once.

---

## agent_list.py

List agents with routing profile, hierarchy group, and security profile details.

```bash
# All agents
python agent_list.py --instance-id <UUID> --region us-east-1

# Search by username substring
python agent_list.py --instance-id <UUID> --search "john"

# Filter by routing profile
python agent_list.py --instance-id <UUID> --routing-profile "Support"

# Export to CSV
python agent_list.py --instance-id <UUID> --csv agents.csv
```

**Required IAM:** `connect:ListUsers`, `connect:DescribeUser`, `connect:DescribeRoutingProfile`, `connect:DescribeUserHierarchyGroup`, `connect:DescribeSecurityProfile`

---

## agent_activity.py

Agent handle time and activity report by date range or named period.

```bash
# Named period
python agent_activity.py --instance-id <UUID> --period last-month

# Custom date range
python agent_activity.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-31

# Filter to a specific agent
python agent_activity.py --instance-id <UUID> --period last-month --agent jsmith

# Export to CSV
python agent_activity.py --instance-id <UUID> --period last-month --output activity.csv
```

Named periods: `today`, `yesterday`, `this-week`, `last-week`, `this-month`, `last-month`

**Required IAM:** `connect:ListUsers`, `connect:DescribeUser`, `connect:GetMetricDataV2`

---

## connectToolbox.py

Interactive terminal menu that wraps all tools above.

```bash
python connectToolbox.py
```

| Key | Action |
|---|---|
| `↑` / `↓` | Move selection |
| `Enter` | Run selected tool |
| `1` – `6` | Jump directly to a tool |
| `q` | Quit |

After selecting a tool, the menu prompts for each argument one at a time. Optional fields can be left blank to use defaults. When the tool finishes, press Enter to return to the menu.

Every run is logged to `~/logs/connecttools.log` with timestamp, exit code, duration, and the full argument list.

Works on both Linux (AWS CloudShell) and Windows (local — see [winpty requirement](#winpty-requirement) above).

---

## IAM Permissions Summary

| Tool | Permissions Required |
|---|---|
| contacts_handled | `connect:ListQueues`, `connect:GetMetricDataV2`, `sts:GetCallerIdentity` |
| contact_inspect | `connect:DescribeContact`, `connect:GetContactAttributes`, `connect:ListContactReferences`, `connect:ListRealtimeContactAnalysisSegmentsV2`, `connect:DescribeQueue`, `connect:DescribeUser` |
| contact_search | `connect:SearchContacts` |
| contact_recordings | `connect:DescribeContact`, `connect:ListInstanceStorageConfigs`, `s3:ListBucket`, `s3:GetObject` |
| contact_logs | `connect:DescribeContact`, `connect:DescribeInstance`, `logs:FilterLogEvents` |
| lambda_tracer | `connect:DescribeContact`, `connect:DescribeInstance`, `logs:FilterLogEvents` (Connect + Lambda log groups) |
| export_flow | `connect:ListContactFlows`, `connect:DescribeContactFlow` |
| flow_to_chart | *(no AWS calls)* |
| log_insights | `logs:DescribeLogGroups`, `logs:StartQuery`, `logs:GetQueryResults` |
| cid_journey | *(no AWS calls)* |
| agent_list | `connect:ListUsers`, `connect:DescribeUser`, `connect:DescribeRoutingProfile`, `connect:DescribeUserHierarchyGroup`, `connect:DescribeSecurityProfile` |
| agent_activity | `connect:ListUsers`, `connect:DescribeUser`, `connect:GetMetricDataV2` |
