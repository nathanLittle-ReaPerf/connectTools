# contact_timeline.py

Build a chronological event timeline for a single Amazon Connect contact by stitching together contact metadata milestones, every flow block execution from CloudWatch flow logs, Lambda invocations, and (optionally) Contact Lens transcript turns. Each event is shown with a `T+` offset from contact initiation so you can see exactly how long each step took. Output can be a human-readable columnar table, JSON to stdout, or a JSON file.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --region us-east-1
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--contact-id` | Contact UUID to build the timeline for (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--log-group` | Override the auto-discovered Connect CloudWatch log group (default: `/aws/connect/<instance-alias>`) |
| `--transcript` | Include Contact Lens transcript turns as `LENS` events in the timeline |
| `--json` | Print the full timeline as JSON to stdout |
| `--output` | Write JSON timeline to a file |

### Examples

```bash
# Human-readable timeline
python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Include transcript turns from Contact Lens
python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --transcript

# JSON output
python contact_timeline.py --instance-id <UUID> --contact-id <UUID> --json

# Override log group and save to file
python contact_timeline.py --instance-id <UUID> --contact-id <UUID> \
    --log-group /aws/connect/my-instance --output timeline.json
```

## Output

**Human-readable** — columnar table with a summary header:

```
  ────────────────────────────────────────────────────────────────────────
  CONTACT TIMELINE   <contact-id>
  ────────────────────────────────────────────────────────────────────────
  Channel: VOICE    Duration: 3m 42s    Queue: Billing    Agent: Jane Smith
  Log group: /aws/connect/my-instance
  18 events  (14 flow block(s), 2 Lambda invocation(s))

  OFFSET     KIND     EVENT                         DETAIL
  ---------  -------  ----------------------------  ------------------------------
  T+00:00    CONTACT  Contact initiated             INBOUND  VOICE
  T+00:01    FLOW     Play prompt                   Main IVR
  T+00:04    FLOW     Check attribute               Language=en → English
  T+00:06    LAMBDA   AuthFunction                  Success  ·  Main IVR
  T+01:12    CONTACT  Entered queue                 Billing
  T+02:44    CONTACT  Agent connected               Jane Smith
  T+03:42    CONTACT  Contact disconnected          AGENT_HANGUP
```

Event kinds are colour-coded: CONTACT (bold), LAMBDA (yellow), LENS (grey), FLOW (plain).

**JSON (`--json` or `--output`):** Document containing `contact_id`, `contact`, `names`, `log_group`, `lens_available`, `event_count`, and an `events` array. Each event has `offset_s`, `offset_fmt`, `timestamp`, `kind`, `label`, and `detail`. Raw counts of flow log events and Lens segments are included under `raw`.

## Key Behaviours

- **Four event kinds** are stitched into a single sorted timeline: `CONTACT` (milestones from `DescribeContact`), `FLOW` (every block from CloudWatch logs), `LAMBDA` (Lambda invocations parsed from flow logs), and `LENS` (transcript turns, only with `--transcript`).
- **Contact milestones** sourced from `DescribeContact`: initiation, queue entry (`EnqueueTimestamp`), agent connection (`ConnectedToAgentTimestamp`), and disconnect.
- **Flow log window** is from 2 minutes before contact initiation to 5 minutes after disconnect (or now for live contacts). Flow logs are filtered by `ContactId` using a CloudWatch filter pattern.
- **Log group auto-discovery** resolves the Connect log group from `DescribeInstance` → instance alias. Use `--log-group` to override if casing doesn't match.
- **Missing flow logs** — if no log events are found, the tool distinguishes between "flow logging not enabled on this instance" and "contact not found in logs (flow logging may not be enabled on specific flows)" and prints the appropriate diagnostic.
- **Contact Lens** — transcript turns are fetched when `--transcript` is set, or always when `--json`/`--output` is used (for completeness). The 24-hour retention window is checked; expired contacts skip the Lens fetch.
- **Lens transcript offsets** — voice uses `BeginOffsetMillis`; chat uses `AbsoluteTime`.
- Queue and agent IDs are resolved to names via `DescribeQueue` and `DescribeUser` for the summary header.

## Required IAM Permissions

```
connect:DescribeContact
connect:DescribeInstance
connect:DescribeQueue
connect:DescribeUser
connect:ListRealtimeContactAnalysisSegments
logs:FilterLogEvents   (on /aws/connect/<instance-alias>)
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeContact` | Fetch contact milestones (initiation, queue entry, agent connection, disconnect) |
| `DescribeInstance` | Resolve instance alias for auto-discovering the Connect CloudWatch log group |
| `DescribeQueue` | Resolve queue ID to name for the summary header |
| `DescribeUser` | Resolve agent ID to name for the summary header |
| `FilterLogEvents` (Connect) | Fetch all flow-execution log entries for the contact's time window |
| `ListRealtimeContactAnalysisSegmentsV2` | Fetch Contact Lens transcript turns (when `--transcript` or JSON output requested) |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: four-kind timeline (CONTACT/FLOW/LAMBDA/LENS), T+ offset display, flow log auto-discovery, JSON output |
