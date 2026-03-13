# lambda_tracer.py

Trace every Lambda function invoked during an Amazon Connect contact. Pulls Connect flow-execution logs to find Lambda invocations, then fetches the actual Lambda CloudWatch logs for each function around the invocation timestamp.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --region us-east-1
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--contact-id` | Contact UUID to trace (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--log-group` | Override auto-discovered Connect log group (default: `/aws/connect/<instance-alias>`) |
| `--output` | Write JSON output to file (default: print human-readable to stdout) |
| `--json` | Print JSON to stdout instead of human-readable output |

### Examples

```bash
# Human-readable trace
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Save to JSON file
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --output trace.json

# Pipe JSON to jq
python lambda_tracer.py --instance-id <UUID> --contact-id <UUID> --json | jq '.invocations[].function_name'
```

## Output

**Human-readable** — one block per Lambda invocation:

```
  ──────────────────────────────────────────────────────────────────
  LAMBDA TRACE   <contact-id>
  ──────────────────────────────────────────────────────────────────

  2 invocation(s) found.

  [1] MyAuthFunction
       ARN       : arn:aws:lambda:us-east-1:123456789012:function:MyAuthFunction
       Invoked   : 2026-03-07 15:19:01.234 UTC
       Flow      : Main IVR
       Result    : Success
       Response  : {"statusCode":"200","authenticated":"true"}

       Lambda logs (±30s window):
         15:19:01.100  START RequestId: abc-123 Version: $LATEST
         15:19:01.230  Checking auth for customer: 555-1234
         15:19:01.233  Auth result: true
         15:19:01.235  END RequestId: abc-123
         15:19:01.235  REPORT RequestId: abc-123  Duration: 135.00 ms ...
```

**JSON (`--json` or `--output`):**

```json
{
  "contact_id": "...",
  "connect_log_group": "/aws/connect/myInstance",
  "invocation_count": 2,
  "invocations": [
    {
      "function_arn": "arn:aws:lambda:...",
      "function_name": "MyAuthFunction",
      "invoked_at": "2026-03-07T15:19:01.234000+00:00",
      "result": "Success",
      "connect_response": { "statusCode": "200" },
      "flow_name": "Main IVR",
      "lambda_log_count": 5,
      "lambda_logs": [ { "timestamp": "...", "message": "..." } ]
    }
  ]
}
```

## Key Behaviours

- **Connect log parsing** — scans flow-execution logs for `InvokeExternalResource` / `InvokeLambdaFunction` block types, which is how Connect records Lambda calls in CloudWatch.
- **Lambda log window** — searches `/aws/lambda/<function-name>` ±30 seconds around the Connect-reported invocation timestamp. This is accurate for most contacts; high-concurrency functions may include log lines from concurrent invocations.
- **Log group auto-discovery** — same mechanism as `contact_logs.py`: derives the Connect log group from the instance alias via `DescribeInstance`. Override with `--log-group` if casing doesn't match. The toolbox saves the correct name per instance.
- **Missing Lambda logs** — if no log events are found for a Lambda, a note is printed. Common causes: IAM missing `logs:FilterLogEvents` on the Lambda log group, or log retention expired.

## Required IAM Permissions

```
connect:DescribeContact
connect:DescribeInstance
logs:FilterLogEvents   (on /aws/connect/<instance-alias>)
logs:FilterLogEvents   (on /aws/lambda/<function-name> for each invoked function)
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeContact` | Get initiation/disconnect timestamps for time-bounded search |
| `DescribeInstance` | Resolve instance alias → Connect log group name |
| `FilterLogEvents` (Connect) | Pull flow-execution logs and find Lambda invocation entries |
| `FilterLogEvents` (Lambda) | Fetch Lambda execution logs around each invocation timestamp |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: Connect log parsing, Lambda log correlation, human-readable and JSON output |
