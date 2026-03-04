# contact_search.py

Search Amazon Connect contacts by time range and optional filters, then export to CSV or JSON.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python contact_search.py --instance-id <UUID> --start <DATE> --end <DATE> [options]
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--start` | Start of time range — YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS UTC (required) |
| `--end` | End of time range — YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS UTC (required) |
| `--time-type` | Timestamp field to filter on: `INITIATION_TIMESTAMP` (default), `DISCONNECT_TIMESTAMP`, `SCHEDULED_TIMESTAMP` |
| `--channel` | Filter by channel (repeatable): `VOICE`, `CHAT`, `TASK`, `EMAIL` |
| `--queue` | Filter by queue ID (repeatable) |
| `--agent` | Filter by agent user ID (repeatable) |
| `--initiation-method` | Filter by initiation method (repeatable): `INBOUND`, `OUTBOUND`, `TRANSFER`, `CALLBACK`, `API`, `QUEUE_TRANSFER`, `EXTERNAL_OUTBOUND`, `MONITOR`, `DISCONNECT` |
| `--attribute` | Filter by contact attribute as `KEY=VALUE` (repeatable). Multiple values use MATCH_ALL logic. |
| `--sort-by` | Sort field (default: `INITIATION_TIMESTAMP`). Also: `SCHEDULED_TIMESTAMP`, `DISCONNECT_TIMESTAMP`, `HANDLE_TIME`, `AGENT_INTERACTION_DURATION`, `CUSTOMER_HOLD_DURATION` |
| `--sort-order` | `ASCENDING` or `DESCENDING` (default: `DESCENDING`) |
| `--limit` | Maximum number of contacts to return (default: all) |
| `--output` | CSV output path (default: `contacts_YYYYMMDD_HHMMSS.csv`) |
| `--json` | Emit raw JSON array to stdout instead of CSV |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |

### Examples

```bash
# All contacts initiated on a date
python contact_search.py --instance-id f79da75c-... --start 2026-03-01 --end 2026-03-02

# Voice inbound contacts for a specific queue, written to a named file
python contact_search.py --instance-id f79da75c-... --start 2026-03-01 --end 2026-03-02 \
    --channel VOICE --initiation-method INBOUND --queue <QUEUE-ID> --output results.csv

# Filter by a custom contact attribute
python contact_search.py --instance-id f79da75c-... --start 2026-03-01 --end 2026-03-02 \
    --attribute Department=Billing

# Multiple attribute filters (all must match)
python contact_search.py --instance-id f79da75c-... --start 2026-03-01 --end 2026-03-02 \
    --attribute Department=Billing --attribute Language=en

# First 500 contacts oldest-first, JSON output
python contact_search.py --instance-id f79da75c-... --start 2026-03-01 --end 2026-03-04 \
    --sort-order ASCENDING --limit 500 --json | jq '.[0].Id'

# Filter by disconnect timestamp instead of initiation
python contact_search.py --instance-id f79da75c-... --start 2026-03-01 --end 2026-03-02 \
    --time-type DISCONNECT_TIMESTAMP
```

## CSV Columns

| Column | Description |
|---|---|
| `contact_id` | Contact UUID |
| `channel` | `VOICE`, `CHAT`, `TASK`, or `EMAIL` |
| `initiation_method` | How the contact started (e.g. `INBOUND`, `OUTBOUND`) |
| `initiation_timestamp` | When the contact started (UTC ISO 8601) |
| `disconnect_timestamp` | When the contact ended (UTC ISO 8601) |
| `duration_seconds` | Total duration in seconds (blank if contact still active) |
| `queue_id` | Queue UUID the contact was routed to |
| `enqueue_timestamp` | When the contact entered the queue |
| `agent_id` | Agent user UUID |
| `connected_to_agent_timestamp` | When the agent connected |
| `customer_endpoint` | Customer phone number or chat endpoint with type |
| `system_endpoint` | Number the customer dialed (DNIS) with type |
| `disconnect_reason` | Why the contact ended (e.g. `CUSTOMER_DISCONNECT`) |
| `initial_contact_id` | Contact ID of the first leg (same as contact_id if not transferred) |
| `previous_contact_id` | Contact ID of the previous leg in a transfer chain |

## Notes

- **Throttle:** `SearchContacts` is limited to 0.5 requests/second. The script sleeps 2 seconds between pages and prints live progress (`Fetched N / TOTAL contacts`) to stderr.
- **Time range:** Both `--start` and `--end` are interpreted as UTC. A bare date like `2026-03-01` means midnight UTC of that day.
- **Multiple filter flags:** Repeating `--channel`, `--queue`, `--agent`, or `--initiation-method` adds values to the same filter list (OR logic within each field). Multiple `--attribute` pairs use MATCH_ALL (AND logic across attributes).
- **`--json` and `--output`:** When `--json` is set, output goes to stdout and `--output` is ignored.

## Required IAM Permissions

```
connect:SearchContacts
```

## APIs Used

| API | Purpose |
|---|---|
| `SearchContacts` | Paginated contact search with filtering and sorting |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: SearchContacts with channel, queue, agent, initiation method, and custom attribute filters; CSV and JSON output; 2s inter-page throttle |
