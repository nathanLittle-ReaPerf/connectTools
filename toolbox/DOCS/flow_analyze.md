# flow_analyze.py

Scan Amazon Connect contact flows for hard errors and optimization suggestions in a single pass.
Combines what were formerly `flow_scan.py` (structural errors) and `flow_optimize.py` (best-practice suggestions).

## Dependencies

No pip install required beyond boto3 (pre-installed in AWS CloudShell).

## Usage

```bash
python flow_analyze.py FLOW_JSON [OPTIONS]
python flow_analyze.py --instance-id UUID --name NAME [OPTIONS]
python flow_analyze.py --instance-id UUID --all [OPTIONS]
```

## Analysis modes (default: both)

| Flag | What it checks |
|---|---|
| *(default)* | Runs both scan and optimize passes |
| `--scan` | Error scanner only — broken refs, dead ends, missing error handlers |
| `--optimize` | Optimization suggestions only — UX, reliability, structure, maintainability |

## Options

| Flag | Description |
|---|---|
| `FLOW_JSON` | Local exported flow JSON (from `export_flow.py`) |
| `--instance-id` | Amazon Connect instance UUID |
| `--name` | Flow name to analyze (case-insensitive substring) |
| `--all` | Analyze all flows in the instance |
| `--type` | Filter by flow type with `--all` (e.g. `CONTACT_FLOW`) |
| `--detail` | Show per-block breakdown in bulk mode |
| `--csv FILE` | Write scan issues to CSV |
| `--json` | Emit raw JSON |
| `--region` | AWS region |
| `--profile` | Named AWS profile for local use |

## Examples

```bash
# Local file — scan + optimize (default)
python flow_analyze.py Main_IVR.json

# Scan only, live instance
python flow_analyze.py --instance-id <UUID> --name "Main IVR" --scan

# Full analysis of all flows with per-block detail
python flow_analyze.py --instance-id <UUID> --all --detail

# Bulk JSON, flows with scan errors
python flow_analyze.py --instance-id <UUID> --all --json \
  | jq '.flows[] | select(.scan.issue_count > 0)'

# Bulk JSON, flows with optimization suggestions
python flow_analyze.py --instance-id <UUID> --all --json \
  | jq '.flows[] | select(.optimize.suggestion_count > 0)'
```

## Scan findings (--scan)

| Severity | Kind | Description |
|---|---|---|
| ERROR | `broken_start` | StartAction references a missing block |
| ERROR | `broken_target` | Transition points to a missing block |
| ERROR | `dead_end` | Non-terminal block with no outgoing transitions |
| ERROR | `missing_lambda_arn` | InvokeLambdaFunction block with empty ARN |
| WARN | `missing_error_branch` | Lambda/Transfer/InvokeFlow block with no error handler |
| WARN | `missing_default` | Decision block has conditions but no default branch |
| WARN | `unreachable` | Block never referenced by any other block |
| WARN | `missing_queue` | SetQueue block with no queue configured |

## Optimize suggestions (--optimize)

| Level | Category | Description |
|---|---|---|
| WARN | UX | GetUserInput with > 5 menu options |
| WARN | UX | GetUserInput with no error handler (invalid key / timeout) |
| SUGGEST | Reliability | Transfer to queue but no staffing check |
| SUGGEST | Reliability | No hours-of-operation check in a routing flow |
| SUGGEST | Structure | Flow has > 40 blocks — consider sub-flows |
| SUGGEST | Structure | Back-to-back Lambda calls — consider combining |
| SUGGEST | Maintainability | Same prompt text in 3+ blocks — consider a shared prompt |

## JSON output structure

```json
{
  "flow_count": 12,
  "total_issues": 3,
  "total_suggestions": 7,
  "flows": [
    {
      "flow": "Main IVR",
      "block_count": 42,
      "scan": {
        "issue_count": 1,
        "errors": 1,
        "warnings": 0,
        "issues": [{ "severity": "ERROR", "kind": "dead_end", ... }]
      },
      "optimize": {
        "suggestion_count": 2,
        "warns": 1,
        "suggestions": [{ "level": "WARN", "category": "ux", ... }]
      }
    }
  ]
}
```

Only `scan` / `optimize` keys are present for the passes that were run.

## IAM permissions

- `connect:ListContactFlows`
- `connect:DescribeContactFlow`
