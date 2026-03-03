# export_flow.py

Export an Amazon Connect contact flow to JSON by name or ARN.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python export_flow.py --instance-id <UUID> --name <NAME> [options]
```

| Flag | Description |
|---|---|
| `--instance-id` | Connect instance UUID (required) |
| `--name` | Flow name to search for — case-insensitive substring by default |
| `--arn` | Full flow ARN — exact match; mutually exclusive with `--name` |
| `--exact` | Require exact name match (case-insensitive); no effect with `--arn` |
| `--type` | Restrict search to one flow type (see below) |
| `--output` | Write exported JSON to this file path |
| `--stdout` | Print exported JSON to stdout (pipe-friendly) |
| `--list` | List matching flows without exporting |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |

### Flow types

`CONTACT_FLOW`, `CUSTOMER_QUEUE`, `CUSTOMER_HOLD`, `CUSTOMER_WHISPER`, `AGENT_HOLD`, `AGENT_WHISPER`, `OUTBOUND_WHISPER`, `AGENT_TRANSFER`, `QUEUE_TRANSFER`, `CAMPAIGN`

### Examples

```bash
# Export a flow to <Flow Name>.json
python export_flow.py --instance-id f79da75c-... --name "Main IVR"

# Exact name match
python export_flow.py --instance-id f79da75c-... --name "Main IVR" --exact

# Write to a specific file
python export_flow.py --instance-id f79da75c-... --name "Main IVR" --output ./flows/main_ivr.json

# Print to stdout (pipe to jq, etc.)
python export_flow.py --instance-id f79da75c-... --name "Main IVR" --stdout | jq '.metadata'

# List all flows
python export_flow.py --instance-id f79da75c-... --list

# List flows with name filter
python export_flow.py --instance-id f79da75c-... --list --name "IVR"

# List flows of a specific type
python export_flow.py --instance-id f79da75c-... --list --type CONTACT_FLOW

# Export list to JSON file
python export_flow.py --instance-id f79da75c-... --list --output flows_list.json

# Export by ARN
python export_flow.py --instance-id f79da75c-... --arn arn:aws:connect:us-west-2:...
```

## Output Format

Exported files use a self-describing envelope:

```json
{
  "metadata": {
    "name": "Main IVR",
    "id": "...",
    "arn": "arn:aws:connect:...",
    "type": "CONTACT_FLOW",
    "status": "PUBLISHED",
    "state": "ACTIVE",
    "description": "...",
    "last_modified_time": "...",
    "flow_content_sha256": "..."
  },
  "content": { ... }
}
```

The `content` field is the parsed flow definition (Actions array, StartAction, etc.) and can be passed directly to `flow_to_chart.py`.

## Default output filename

If `--output` is not specified, the file is saved as `<Flow Name>.json` with non-alphanumeric characters replaced by underscores.

## Required IAM Permissions

```
connect:ListContactFlows
connect:DescribeContactFlow
```

## APIs Used

| API | Purpose |
|---|---|
| `ListContactFlows` | Discover flows by name or list all |
| `DescribeContactFlow` | Fetch full flow definition including content |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: ListContactFlows, DescribeContactFlow, name substring search, `--stdout` flag |
| v2 | `--region` defaults to CloudShell/session region instead of hardcoded `us-east-1` |
| v3 | Added `--arn` flag for exact ARN-based lookup |
| v4 | `--list` with `--output` exports the flow list as JSON |
