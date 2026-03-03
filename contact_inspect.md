# contact_inspect.py

Pull all available data for a single Amazon Connect contact in one shot.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python contact_inspect.py --instance-id <UUID> --contact-id <UUID> [options]
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--contact-id` | Contact UUID to inspect (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--transcript` | Print full Contact Lens transcript turns |
| `--json` | Emit raw merged JSON (pipe-friendly) |

### Examples

```bash
# Standard human-readable output
python contact_inspect.py --instance-id f79da75c-... --contact-id abc123-...

# Include full transcript
python contact_inspect.py --instance-id f79da75c-... --contact-id abc123-... --transcript

# Pipe to jq
python contact_inspect.py --instance-id f79da75c-... --contact-id abc123-... --json | jq '.contact.Channel'
```

## Output Sections

**CONTACT** — Core metadata: channel, initiation method, timestamps, duration, queue name + ID, agent name + ID, customer endpoint, system endpoint (the number the customer dialed).

**TRANSFER CHAIN** — If the contact was transferred, walks `PreviousContactId` backwards to show the full chain oldest-to-current.

**CONTACT ATTRIBUTES** — Custom key/value pairs set during the contact flow (from `GetContactAttributes`).

**REFERENCES** — Links attached to the contact: recordings, Contact Lens analysis output, attachments, etc.

**CONTACT LENS** — Transcript turn count, per-role sentiment summary (positive/neutral/negative counts), detected issues, matched categories, and post-contact summary. Use `--transcript` to print all turns with timestamps and role labels.

> Contact Lens data has a **24-hour retention window**. For older contacts the section will explain why data is unavailable and suggest checking the S3 export bucket.

## Required IAM Permissions

```
connect:DescribeContact
connect:GetContactAttributes
connect:ListContactReferences
connect:ListRealtimeContactAnalysisSegments
connect:DescribeQueue
connect:DescribeUser
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeContact` | Core contact metadata |
| `GetContactAttributes` | Custom flow-set attributes |
| `ListContactReferences` | Recordings, analysis links, attachments |
| `DescribeQueue` | Resolve queue ID → name |
| `DescribeUser` | Resolve agent ID → full name |
| `ListRealtimeContactAnalysisSegments` | Voice Contact Lens transcript + sentiment |
| `ListRealtimeContactAnalysisSegmentsV2` | Chat/email Contact Lens transcript + sentiment |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: DescribeContact, GetContactAttributes, ListContactReferences, Contact Lens (voice + chat), transfer chain walking, `--json` and `--transcript` flags |
| v2 | `--region` defaults to CloudShell/session region instead of hardcoded `us-east-1` |
| v3 | Queue name resolved via `DescribeQueue` and displayed above queue ID; agent full name resolved via `DescribeUser` and displayed above agent ID |
| v4 | System endpoint added to output |
