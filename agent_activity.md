# agent_activity.py

Pull per-agent activity metrics from Amazon Connect for a given time period and write the results to a CSV file. Metrics include contacts handled, occupancy percentage, online time, on-contact time, idle time, non-productive time, error status time, average handle time, average after-contact work time, and average talk time. Supports named period shortcuts or a custom date range, and can be filtered to specific agents by login name.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python agent_activity.py --instance-id <UUID> --period last-month
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (mutually exclusive with `--instance-arn`) |
| `--instance-arn` | Amazon Connect instance ARN (mutually exclusive with `--instance-id`) |
| `--period` | Named period shortcut: `today`, `yesterday`, `this-week`, `last-week`, `this-month`, `last-month` (mutually exclusive with `--start`) |
| `--start` | Custom range start date inclusive, format `YYYY-MM-DD` (mutually exclusive with `--period`) |
| `--end` | Custom range end date inclusive, format `YYYY-MM-DD` — defaults to today when `--start` is used |
| `--agent` | Filter to a specific agent login name (repeatable — use multiple times for multiple agents) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--output` | CSV output path (default: auto-named `agent_activity_<period>_<date>.csv` in current directory) |

### Examples

```bash
# Last month, all agents
python agent_activity.py --instance-id <UUID> --period last-month

# This week, one agent
python agent_activity.py --instance-id <UUID> --period this-week --agent jsmith

# Multiple agents
python agent_activity.py --instance-id <UUID> --period this-week --agent jsmith --agent bjones

# Custom date range
python agent_activity.py --instance-id <UUID> --start 2025-01-01 --end 2025-01-31

# Yesterday, write to specific file
python agent_activity.py --instance-id <UUID> --period yesterday --output /tmp/report.csv
```

## Output

**CSV only** — one row per agent with data in the time window. Columns:

| Column | Description |
|---|---|
| `AgentUsername` | Login name |
| `AgentId` | Connect user UUID |
| `ContactsHandled` | Total contacts handled |
| `Occupancy_pct` | Agent occupancy percentage |
| `OnlineTime_sec` | Total online time in seconds |
| `OnContactTime_sec` | Total on-contact time in seconds |
| `IdleTime_sec` | Total idle time in seconds |
| `NonProductiveTime_sec` | Total non-productive time in seconds |
| `ErrorStatusTime_sec` | Total error-status time in seconds |
| `AvgHandleTime_sec` | Average handle time in seconds |
| `AvgACW_sec` | Average after-contact work time in seconds |
| `AvgTalkTime_sec` | Average talk time in seconds |

The filename is printed to stdout on completion. Progress lines (agents found, routing profiles found, agents with activity) are printed to stderr.

## Key Behaviours

- Accepts `--instance-id` or `--instance-arn` (mutually exclusive); when using `--instance-id` the ARN is resolved via `DescribeInstance` for the metrics API call.
- Named periods resolve to UTC boundaries: `this-week` starts on Monday, `last-week` is the full preceding Monday-to-Sunday week.
- The 93-day retention limit is enforced client-side — requests with a start date older than 93 days exit with a clear error message before making any metrics calls.
- When filtering by `--agent`, user IDs are resolved from login names before calling the metrics API; an unknown login exits with an error.
- When no `--agent` filter is given, metrics are fetched grouped by routing profile (batched in chunks of 100) then grouped by `AGENT` dimension.
- Agents with zero activity in the period appear in the CSV with zero values.
- Output file is auto-named `agent_activity_<period_label>_<YYYY-MM-DD>.csv` unless `--output` is provided.

## Required IAM Permissions

```
connect:DescribeInstance
connect:ListUsers
connect:ListRoutingProfiles
connect:GetMetricDataV2
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeInstance` | Resolve instance ID to ARN (required by GetMetricDataV2) |
| `ListUsers` | Build a user-ID-to-username map for resolving agent names in CSV output |
| `ListRoutingProfiles` | List routing profile IDs used as filter keys when no `--agent` specified |
| `GetMetricDataV2` | Fetch per-agent metrics over the requested time window, grouped by AGENT dimension |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: named period shortcuts, custom date range, per-agent metrics CSV output, --agent filter, 93-day retention guard |
