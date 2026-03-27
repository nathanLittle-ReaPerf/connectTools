# contact_logs.py

Download CloudWatch flow-execution logs for an Amazon Connect contact ID.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python contact_logs.py --instance-id <UUID> --contact-id <UUID> [options]
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--contact-id` | Contact UUID (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--log-group` | Override auto-discovered log group (default: `/aws/connect/<instance-alias>`) |
| `--text` | Plain-text output instead of JSON |
| `--output` | Output file path (default: `<contact-id>_logs.json` or `.txt`) |

### Examples

```bash
# JSON output (default)
python contact_logs.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Plain text
python contact_logs.py --instance-id <UUID> --contact-id <UUID> --text

# Override log group if auto-discovery gets the casing wrong
python contact_logs.py --instance-id <UUID> --contact-id <UUID> --log-group /aws/connect/myInstance

# Pipe JSON to jq
python contact_logs.py --instance-id <UUID> --contact-id <UUID> | jq '.events[].message'
```

## Output

**JSON (default)** — structured document with contact metadata and a parsed `events` array:

```json
{
  "contact_id": "...",
  "log_group": "/aws/connect/myInstance",
  "window": { "start": "...", "end": "..." },
  "event_count": 42,
  "events": [
    {
      "timestamp": "2026-03-07T15:19:00.123Z",
      "log_stream": "...",
      "message": { ... }
    }
  ]
}
```

Each `message` is parsed from JSON if possible (Connect log entries are JSON), otherwise returned as `{"raw": "..."}`.

**Text (`--text`)** — one line per event, timestamp + raw message:

```
2026-03-07 15:19:00.123 UTC  {"ContactId":"...","ContactFlowId":"...",...}
```

## Key Behaviours

- **Log group auto-discovery** — calls `DescribeInstance` to get the instance alias and constructs `/aws/connect/<alias>`. If the alias casing doesn't match the actual log group name, use `--log-group` to override. The toolbox saves the correct log group per instance ID so you only need to enter it once.
- **Precise time window** — uses `DescribeContact` to bound the search to contact initiation −2 min → disconnect +5 min, keeping queries fast.
- **Pagination** — `FilterLogEvents` is fully paginated; contacts with many flow branches return all events.

## Required IAM Permissions

```
connect:DescribeContact
connect:DescribeInstance
logs:FilterLogEvents
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeContact` | Get initiation/disconnect timestamps for time-bounded search |
| `DescribeInstance` | Resolve instance alias → log group name |
| `FilterLogEvents` | Paginated log event retrieval filtered by contact ID |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: auto-discovers log group, time-bounded FilterLogEvents, JSON and text output |
