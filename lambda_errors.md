# lambda_errors.py

Aggregate Lambda errors from CloudWatch Logs over a configurable time window. Searches the function's `/aws/lambda/<name>` log group for error events, classifies them by error type (exception class, timeout, or generic error), and groups occurrences. When `--instance-id` is provided, also scans Connect flow logs (`/aws/connect/<alias>`) for Lambda invocation failures recorded by Connect — catching errors that never produce a Lambda log entry (for example, invocation-level failures or timeouts at the Connect level). Results from both sources are shown in separate sections.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python lambda_errors.py --function <NAME> --region us-east-1
```

| Flag | Description |
|---|---|
| `--function` | Lambda function name, name fragment, or full ARN (required); ARN is parsed automatically to extract the function name |
| `--instance-id` | Amazon Connect instance UUID — when provided, also searches Connect flow logs for Lambda failures |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--log-group` | Override the auto-derived Lambda log group (default: `/aws/lambda/<name>`) |
| `--connect-log-group` | Override the auto-discovered Connect flow log group (default: `/aws/connect/<alias>`) |
| `--period` | Named period shortcut: `today`, `yesterday`, `this-week`, `last-week`, `this-month`, `last-month` (mutually exclusive with `--last` and `--start`) |
| `--last` | Relative time window ending now — e.g. `30m`, `4h`, `7d` (mutually exclusive with `--period` and `--start`) |
| `--start` | Absolute window start, format `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS` (mutually exclusive with `--period` and `--last`) |
| `--end` | Absolute window end (default: now) — used with `--start` |
| `--json` | Emit raw JSON with both Lambda log and Connect flow log results |
| `--csv` | Write per-error CSV to a file (all sources combined) |

### Examples

```bash
# Lambda log errors for the last 24 hours (default window)
python lambda_errors.py --function my-connect-lambda --region us-east-1

# Also check Connect flow logs for this function — yesterday
python lambda_errors.py --function my-connect-lambda \
    --instance-id <UUID> --period yesterday

# Full ARN, last week
python lambda_errors.py \
    --function arn:aws:lambda:us-east-1:123456789012:function:my-fn \
    --instance-id <UUID> --period last-week

# Relative window — last 4 hours
python lambda_errors.py --function my-fn --last 4h

# Custom date range with CSV export
python lambda_errors.py --function my-fn --instance-id <UUID> \
    --start 2026-03-15 --end 2026-03-16 --csv errors.csv

# JSON output — inspect Connect flow section
python lambda_errors.py --function my-fn --instance-id <UUID> --json | jq '.connect_flow'
```

## Output

**Human-readable** — two sections: Lambda log errors and (when `--instance-id` provided) Connect flow log errors. Each section shows a total count and a breakdown by error type, with up to 15 occurrences per type:

```
  ────────────────────────────────────────────────────────────────────────
  LAMBDA ERROR REPORT   my-connect-lambda
  ────────────────────────────────────────────────────────────────────────
  Window   : 2026-03-15 00:00 UTC  →  2026-03-16 00:00 UTC
  Lambda log group : /aws/lambda/my-connect-lambda
  Connect log group: /aws/connect/my-instance

  ── Lambda log errors ──
     12 error event(s)  ·  2 type(s)

     Timeout  (9 occurrence(s))
       2026-03-15 09:12:03 UTC  [abc12345…]
         Task timed out after 15.00 seconds

     ValidationError  (3 occurrence(s))
       ...

  ── Connect flow log errors  (with contact IDs) ──
     3 error event(s)  ·  1 type(s)

     Lambda.ServiceException  (3 occurrence(s))
       2026-03-15 11:05:44 UTC   xxxxxxxx-xxxx-...  [Main IVR]
```

When more than 15 occurrences exist for a type, a note is printed directing to `--csv` or `--json`.

**CSV (`--csv`):** One row per error event from all sources combined. Columns: `source`, `timestamp`, `error_type`, `request_id`, `contact_id`, `flow_name`, `message`.

**JSON (`--json`):** Document with `function`, `lambda_log_group`, `connect_log_group`, `window`, `lambda_logs` (total + by_type), and `connect_flow` (null if no `--instance-id`).

## Key Behaviours

- **Default time window** is the last 24 hours when no `--period`, `--last`, or `--start` flag is given.
- **Error classification** from Lambda logs: timeouts are detected by `"Task timed out"` in the log line; JSON-format errors are parsed for `errorType`/`errorMessage`; Python/Java exception class names are extracted from log lines by regex; anything else is classified as `"Error"`.
- `START`, `END`, and `REPORT` Lambda log lines are always skipped — they are not error events.
- **Connect flow log errors** are identified by parsing `InvokeExternalResource`/`InvokeLambdaFunction` block entries that have an `Error` field. The function name argument is matched case-insensitively against the `FunctionArn` in each log entry. Contact ID and flow name are included in the output for traceability.
- **Log group auto-discovery** for Connect: `DescribeInstance` → instance alias → `/aws/connect/<alias>`. Use `--connect-log-group` to override if casing doesn't match.
- If the Lambda log group does not exist (function has never been invoked or the name is wrong), the tool exits with a diagnostic message.
- The Connect log group search uses `missing_ok=True` — if the log group does not exist, an empty result is returned rather than an error.
- Up to 15 occurrences are shown per error type in human output; use `--csv` or `--json` to see all.

## Required IAM Permissions

```
logs:FilterLogEvents   (on /aws/lambda/<function-name>)
connect:DescribeInstance              (when --instance-id provided)
logs:FilterLogEvents   (on /aws/connect/<instance-alias>)  (when --instance-id provided)
```

## APIs Used

| API | Purpose |
|---|---|
| `FilterLogEvents` (Lambda) | Search `/aws/lambda/<name>` for error-matching log lines |
| `DescribeInstance` | Resolve Connect instance alias for auto-discovering the Connect log group |
| `FilterLogEvents` (Connect) | Search `/aws/connect/<alias>` for Lambda invocation failure entries |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: Lambda log error classification, Connect flow log cross-reference, named/relative/absolute time windows, CSV and JSON output |
