# flow_usage.py

Count how often each contact flow is used over a time window. Uses CloudWatch Logs Insights against the Connect flow-log group to count unique contacts or invocations per flow — useful for understanding traffic distribution, identifying unused flows, and capacity planning.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python flow_usage.py --instance-id <UUID> --region us-east-1
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--log-group NAME` | Override auto-discovered Connect log group (`/aws/connect/<alias>`) |
| `--by contacts\|invocations` | Counting mode (default: `contacts`) |
| `--flow NAME` | Filter to flows matching a case-insensitive substring |
| `--last DURATION` | Relative window: `4h`, `7d`, `30d` (default: `7d`) |
| `--start DATE` | Absolute window start (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`) |
| `--end DATE` | Absolute window end (default: now) |
| `--csv FILE` | Write results to `~/.connecttools/flow_usage/<FILE>` |
| `--json` | Print results as a JSON array to stdout |

### Examples

```bash
# All flows, last 7 days (default)
python flow_usage.py --instance-id <UUID> --region us-east-1

# Count by invocations
python flow_usage.py --instance-id <UUID> --by invocations

# Last 24 hours
python flow_usage.py --instance-id <UUID> --last 24h

# Specific date range
python flow_usage.py --instance-id <UUID> --start 2026-03-01 --end 2026-03-17

# Filter to one flow
python flow_usage.py --instance-id <UUID> --flow "Main IVR"

# Export to CSV
python flow_usage.py --instance-id <UUID> --csv usage.csv

# JSON — pipe to jq
python flow_usage.py --instance-id <UUID> --json | jq '.[] | select(.count > 100)'
```

## Output

```
  ────────────────────────────────────────────────────────────────────────
  FLOW USAGE   dbff2776-6bba-4071-98dc-03c16bf2e6de
  ────────────────────────────────────────────────────────────────────────
  2026-03-10 13:32 → 2026-03-17 13:32 UTC  ·  by contacts

  FLOW                          CONTACTS
  ────────────────────────────  ────────
  Main IVR                         1,842
  Support IVR                        934
  Sales Queue                        401
  After Hours                        217

  4 flow(s)  ·  3,394 total contacts
```

**CSV columns:** `flow`, `count`

**JSON:** array of `{"flow": "...", "count": N}` objects sorted by count descending.

## Counting Modes

| Mode | Query | When to use |
|---|---|---|
| `contacts` (default) | `count_distinct(ContactId)` per flow | How many unique callers/sessions went through this flow |
| `invocations` | unique `(ContactId, ContactFlowId)` pairs per flow | How many times the flow was entered, including re-entries within a single call |

The two modes return the same count in most cases. They differ only when the same contact enters the same flow more than once in a single session (e.g. a flow that loops back to itself or a contact is transferred to a flow they already visited).

## Key Behaviours

- **Default window: 7 days** — longer than other tools because flow traffic trends are more meaningful over time.
- **Logs Insights** — runs an async query against the Connect flow log group; polls until complete (typically a few seconds).
- **Flow name filter** — applied client-side after the query; use `--flow` to scope output without changing the underlying query.
- **Flows with zero contacts** produce no log entries and are not returned — use `instance_snapshot.py --show` to see all configured flows.
- **Log retention** — results are limited by the flow log group's retention setting. If the requested window predates the retention period, results will be partial without warning.

## Required IAM Permissions

```
connect:DescribeInstance
logs:StartQuery
logs:GetQueryResults
logs:StopQuery
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeInstance` | Resolve instance alias to discover the Connect log group name |
| `StartQuery` | Submit Logs Insights query against `/aws/connect/<alias>` |
| `GetQueryResults` | Poll until query completes and retrieve aggregated results |

## Changelog

| Version | Change |
|---|---|
| Initial | Count contacts or invocations per flow via Logs Insights; filters, CSV/JSON output |
