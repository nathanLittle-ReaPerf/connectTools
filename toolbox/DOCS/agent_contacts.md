# agent_contacts.py — CONTACTS_HANDLED Per Agent

Reports `CONTACTS_HANDLED` broken down by agent login ID for a given calendar month. Each row shows contacts handled via standard queues, agent (personal) queues, and a combined total. Results are sorted by total descending.

```bash
# Default: previous calendar month
python agent_contacts.py --instance-id <UUID> --region us-east-1

# Specific month
python agent_contacts.py --instance-id <UUID> --month 2026-03

# With timezone
python agent_contacts.py --instance-id <UUID> --timezone America/Chicago

# Export to CSV
python agent_contacts.py --instance-id <UUID> --csv agents.csv

# JSON output
python agent_contacts.py --instance-id <UUID> --json | jq '.agents[] | select(.total > 100)'

# With a named AWS profile (local dev)
python agent_contacts.py --instance-id <UUID> --profile my-admin
```

**APIs used:** `DescribeInstance`, `ListQueues`, `ListUsers`, `GetMetricDataV2`

**Required IAM:**
- `connect:DescribeInstance`
- `connect:ListQueues`
- `connect:ListUsers`
- `connect:GetMetricDataV2`

**Key behaviors:**
- Automatically discovers all queue IDs (standard and agent queues) via `ListQueues`
- Groups results by queue type: standard queues vs. agent (personal) queues vs. total
- Sorted by total contacts handled descending
- `--month` accepts `YYYY-MM`; defaults to previous calendar month
- `GetMetricDataV2` retains historical data for approximately 93 days — requests outside this window exit with an error showing the earliest queryable month
- `--timezone` applies to the aggregation window boundaries (default: UTC)
