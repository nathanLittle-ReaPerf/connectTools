# flow_optimize.py

Rule-based optimization suggestions for Amazon Connect contact flows. Checks for UX anti-patterns, reliability gaps, structural complexity, and maintainability issues. Complements `flow_scan.py` (which catches hard errors) — this tool catches softer problems that won't crash a flow but will hurt caller experience or make the flow harder to maintain.

## Dependencies

No pip install required beyond boto3.

## Usage

```bash
python flow_optimize.py FLOW_JSON
python flow_optimize.py --instance-id <UUID> --name "Main IVR" --region us-east-1
python flow_optimize.py --instance-id <UUID> --all
```

| Flag | Description |
|---|---|
| `FLOW_JSON` | Local exported flow JSON file (mutually exclusive with `--all`) |
| `--instance-id` | Amazon Connect instance UUID (required with `--name` or `--all`) |
| `--name NAME` | Flow name to analyse (case-insensitive substring) |
| `--all` | Analyse all flows in the instance |
| `--type TYPE` | Filter by flow type when using `--all` |
| `--region` | AWS region |
| `--profile` | Named AWS profile |
| `--json` | Print results as JSON |

### Examples

```bash
# Local file
python flow_optimize.py Main_IVR.json

# Single flow from instance
python flow_optimize.py --instance-id <UUID> --name "Main IVR" --region us-east-1

# All CONTACT_FLOW type flows
python flow_optimize.py --instance-id <UUID> --all --type CONTACT_FLOW

# JSON — filter flows with suggestions
python flow_optimize.py --instance-id <UUID> --all --json | jq '.flows[] | select(.suggestion_count > 0)'
```

## Checks

### UX
| Check | Level | Description |
|---|---|---|
| Too many menu options | WARN | `GetUserInput` with >5 conditions — callers struggle with long lists |
| No error handler | WARN | `GetUserInput` with no error branch — callers who press an invalid key or time out have no path |

### Reliability
| Check | Level | Description |
|---|---|---|
| No staffing check | SUGGEST | Flow transfers to a queue but has no `CheckStaffingStatus` block |
| No hours check | SUGGEST | `CONTACT_FLOW` type flow routes to a queue but has no `CheckHoursOfOperation` |

### Structure
| Check | Level | Description |
|---|---|---|
| Large flow | SUGGEST | >40 blocks — candidate for sub-flow refactoring |
| Sequential Lambdas | SUGGEST | Back-to-back `InvokeLambdaFunction` blocks — consider combining |

### Maintainability
| Check | Level | Description |
|---|---|---|
| Duplicate prompt text | SUGGEST | Same text in 3+ blocks — consider a shared prompt resource |

## Suggestion Levels

| Level | Meaning |
|---|---|
| `WARN` | Likely to cause poor caller experience or reliability issues |
| `SUGGEST` | Best practice improvement — worth considering |

## Required IAM Permissions

```
connect:ListContactFlows      (with --all or --name)
connect:DescribeContactFlow   (with --all or --name)
```

## Changelog

| Version | Change |
|---|---|
| Initial | UX, reliability, structure, and maintainability checks; local file and live instance modes; bulk --all support |
