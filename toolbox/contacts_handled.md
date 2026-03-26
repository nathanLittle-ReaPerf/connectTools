# contacts_handled.py

Sum **Contacts Handled** for a given calendar month across all queues in an Amazon Connect instance.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python contacts_handled.py --instance-id <UUID> [options]
```

| Flag | Description |
|---|---|
| `--instance-id` | Connect instance UUID — mutually exclusive with `--instance-arn` |
| `--instance-arn` | Full Connect instance ARN — mutually exclusive with `--instance-id` |
| `--month` | Month to report in `YYYY-MM` format (default: previous calendar month) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--timezone` | Timezone for the aggregation window (default: `UTC`) |

### Examples

```bash
# Previous month (default)
python contacts_handled.py --instance-id f79da75c-...

# Specific month
python contacts_handled.py --instance-id f79da75c-... --month 2026-01

# Current month-to-date
python contacts_handled.py --instance-id f79da75c-... --month 2026-03

# Different timezone
python contacts_handled.py --instance-id f79da75c-... --month 2026-02 --timezone America/Chicago
```

### Example output

```
2026-02-01 to 2026-03-01 (UTC): 14,302 Contacts Handled
```

For the current in-progress month, the end date shown will be today rather than the first of next month.

## Notes

- Automatically discovers all STANDARD queues in the instance — no queue configuration needed.
- Batches queue IDs in groups of 100 to stay within API filter limits.
- Data is available for approximately the last **3 months**. Requests for older months will return a clear error rather than a cryptic API exception.
- Requesting the current month returns a month-to-date total (end time is capped at now).

## Required IAM Permissions

```
connect:DescribeInstance
connect:ListQueues
connect:GetMetricDataV2
```

`connect:DescribeInstance` is only needed when using `--instance-id`. If you pass `--instance-arn` directly, it is not required.

## APIs Used

| API | Purpose |
|---|---|
| `DescribeInstance` | Resolve instance ID → ARN |
| `ListQueues` | Discover all STANDARD queue IDs |
| `GetMetricDataV2` | Fetch CONTACTS_HANDLED aggregate |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: GetMetricDataV2 with TOTAL interval, auto queue discovery, `--instance-id` / `--instance-arn` |
| v2 | `--region` defaults to CloudShell/session region instead of required flag |
| v3 | Replaced STS `GetCallerIdentity` with `DescribeInstance` to resolve ARN without cross-service call |
| v4 | Added error handling for all API calls |
| v5 | Added `from __future__ import annotations` for Python 3.8 compatibility |
| v6 | Added `--month YYYY-MM` flag; defaults to previous calendar month |
| v7 | Added 3-month retention check with clear error message; current month capped at now |
