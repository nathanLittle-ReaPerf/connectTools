# connectTools

A set of Python CLI tools for **Amazon Connect**. Each tool is a single self-contained script designed to run in AWS CloudShell with minimal setup.

## Quick Start

```bash
# One-time dependency install (persists across CloudShell sessions)
pip install python-dateutil openpyxl --user

# Launch the interactive menu
python connectToolbox.py
```

The toolbox is an interactive menu launcher — select a tool with arrow keys or a number key, fill in the prompts, and get results. No flags to memorize.

## Tools

| Script | What it does |
|---|---|
| [`contacts_handled.py`](#contacts_handledpy) | Sum CONTACTS_HANDLED across all queues for a given month |
| [`contact_inspect.py`](#contact_inspectpy) | Deep dive on a single contact — metadata, attributes, Lens transcript, transfer chain |
| [`contact_search.py`](#contact_searchpy) | Search contacts by time range and optional filters; export to CSV or JSON |
| [`export_flow.py`](#export_flowpy) | Export a contact flow's JSON definition by name |
| [`flow_to_chart.py`](#flow_to_chartpy) | Convert an exported flow JSON to an interactive flowchart (HTML, Mermaid, or DOT) |
| [`log_insights.py`](#log_insightspy) | Run a CloudWatch Logs Insights query against Connect log groups; export to Excel |
| [`cid_journey.py`](#cid_journeypy) | Render a Cytoscape.js caller journey map from a CID Search Excel export |
| [`connectToolbox.py`](#connecttoolboxpy) | Interactive menu launcher for all tools above |

---

## Setup

### AWS CloudShell (recommended)

1. Open CloudShell from the AWS Console (terminal icon, top right).
2. Upload scripts via **Actions → Upload file**, or clone this repo.
3. Install the one-time dependencies:

```bash
pip install python-dateutil openpyxl --user
```

### Local Development

```bash
pip install boto3 python-dateutil openpyxl
```

Pass `--profile <name>` to any script to use a named AWS profile instead of the session credentials.

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

**Required IAM:** `connect:DescribeContact`, `connect:GetContactAttributes`, `connect:ListContactReferences`, `connect:ListRealtimeContactAnalysisSegments`

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

Works on both Linux (AWS CloudShell) and Windows (local).

---

## IAM Permissions Summary

| Tool | Permissions Required |
|---|---|
| contacts_handled | `connect:ListQueues`, `connect:GetMetricDataV2`, `sts:GetCallerIdentity` |
| contact_inspect | `connect:DescribeContact`, `connect:GetContactAttributes`, `connect:ListContactReferences`, `connect:ListRealtimeContactAnalysisSegments` |
| contact_search | `connect:SearchContacts` |
| export_flow | `connect:ListContactFlows`, `connect:DescribeContactFlow` |
| flow_to_chart | *(no AWS calls)* |
| log_insights | `logs:DescribeLogGroups`, `logs:StartQuery`, `logs:GetQueryResults` |
| cid_journey | *(no AWS calls)* |
