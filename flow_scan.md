# flow_scan.py

Scan Amazon Connect contact flows for configuration issues. Checks for broken block references (invalid transition targets), dead-end non-terminal blocks with no outgoing transitions, missing error handlers on Lambda and queue-transfer blocks, missing default branches on decision blocks, unreachable blocks that are never referenced, empty Lambda ARNs, and unconfigured queues on SetQueue blocks. Works on local exported JSON files, a single named flow fetched live from an instance, or every flow in an instance at once.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
# Scan a local exported flow file
python flow_scan.py flow.json

# Scan a flow from a live instance by name
python flow_scan.py --instance-id <UUID> --name "Main IVR" --region us-east-1

# Scan all flows in an instance
python flow_scan.py --instance-id <UUID> --all
```

| Flag | Description |
|---|---|
| `FLOW_JSON` | Local exported flow JSON file to scan (from `export_flow.py`) — mutually exclusive with `--all` |
| `--instance-id` | Amazon Connect instance UUID — required with `--name` or `--all` |
| `--name` | Flow name to scan from a live instance (case-insensitive substring match) |
| `--all` | Scan all flows in the instance — mutually exclusive with `FLOW_JSON` |
| `--type` | Filter by flow type when using `--all` (e.g. `CONTACT_FLOW`) |
| `--detail` | Show per-block issue breakdown in bulk (`--all`) mode; without this flag only a summary table is shown |
| `--json` | Emit raw JSON with full issue details |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |

### Examples

```bash
# Scan a local exported file
python flow_scan.py flow.json

# Scan a single flow by name from a live instance
python flow_scan.py --instance-id <UUID> --name "Main IVR" --region us-east-1

# Scan every flow in the instance
python flow_scan.py --instance-id <UUID> --all

# Bulk scan with per-block detail for a specific flow type
python flow_scan.py --instance-id <UUID> --all --type CONTACT_FLOW --detail

# JSON output — filter flows with issues
python flow_scan.py --instance-id <UUID> --all --json | jq '.flows[] | select(.issue_count > 0)'
```

## Output

**Single flow (human-readable):**

```
  ────────────────────────────────────────────────────────────────────────
  FLOW SCAN   Main IVR
  ────────────────────────────────────────────────────────────────────────
  24 block(s) scanned   3 issue(s) found   2 ERROR  1 WARN

  "AuthFunction" (InvokeLambdaFunction)
    [WARN]   missing error handler
             No error handler — a failure will leave the contact with no path forward

  "abc123…" (CheckAttribute)
    [ERROR]  broken block reference
             Default branch → "def456…" not found in flow
```

**Bulk mode (`--all`):** Summary table with one row per flow showing block count and issue counts; clean flows show a green checkmark. Use `--detail` to additionally print the per-block breakdown for all flows that have issues.

**JSON (`--json`):** For a single flow: `{flow, block_count, issue_count, errors, warnings, issues[]}`. For bulk: `{flow_count, flows_with_issues, total_issues, total_errors, total_warnings, flows[]}`.

## Key Behaviours

- **Seven issue checks** are run per block: broken start action, broken transition targets, dead-end blocks, missing error handlers (Lambda/TransferToQueue), missing default branches on decision blocks, unreachable blocks, missing Lambda ARN, and unconfigured queues on SetQueue.
- **Issue severities:** `ERROR` indicates a contact will break or get stuck; `WARN` indicates a potential misconfiguration that may cause unexpected behaviour.
- **Name resolution** — if `instance_snapshot.py` has been run and a snapshot exists for the instance, block identifiers in issue messages are resolved to human-readable names (e.g. `"Main Menu (abc12345…)"` instead of a bare UUID).
- **Local file mode** accepts both the `export_flow.py` envelope format (`{"metadata":..., "content":...}`) and raw flow content JSON directly.
- **Single flow by name** exits with an error if more than one flow matches the name substring.
- **Bulk mode** scans all flows sequentially with a progress counter (`[N/total] FlowName`) on stderr; large instances with hundreds of flows may take a minute.
- Blocks of types `DisconnectParticipant`, `TransferContactToQueue`, `TransferContactToFlow`, and `EndFlowExecution` are treated as terminals — no outgoing transition is required.

## Required IAM Permissions

```
connect:ListContactFlows
connect:DescribeContactFlow
```

## APIs Used

| API | Purpose |
|---|---|
| `ListContactFlows` | List all flows in the instance (used with `--name` or `--all`) |
| `DescribeContactFlow` | Fetch flow content JSON for analysis |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: seven issue checks, single/bulk/local-file modes, ERROR/WARN severities, snapshot-based name resolution, JSON output |
