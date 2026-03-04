# log_insights.py

Query CloudWatch Logs Insights against Amazon Connect log groups and export results to Excel.

## Dependencies

```bash
pip install openpyxl
```

boto3 is pre-installed in CloudShell. openpyxl is a one-time install.

## Usage

```bash
python log_insights.py --query <FILE> --last <DURATION> [options]
python log_insights.py --query <FILE> --start <DATE> [--end <DATE>] [options]
```

| Flag | Description |
|---|---|
| `--query` | Path to a Logs Insights query file (`.sql` or `.txt`) |
| `--log-group` | Log group name — auto-discovers `/aws/connect/` groups if omitted |
| `--last` | Relative time range: `24h`, `7d`, `30m`, `2w` |
| `--start` | Start datetime: `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` |
| `--end` | End datetime (default: now) |
| `--limit` | Max rows returned (default: 1000, max: 10000) |
| `--output` | Output `.xlsx` path (default: `results_<timestamp>.xlsx`) |
| `--list-logs` | List available `/aws/connect/` log groups and exit |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |

### Examples

```bash
# Last 24 hours, auto-detect log group
python log_insights.py --query call_report.sql --last 24h

# Specific date range
python log_insights.py --query call_report.sql --start 2026-03-01 --end 2026-03-02

# Specific log group, save to named file
python log_insights.py --query call_report.sql --last 7d \
  --log-group /aws/connect/myinstance --output march_calls.xlsx

# Increase result limit
python log_insights.py --query call_report.sql --last 30d --limit 5000

# List available Connect log groups
python log_insights.py --list-logs
```

## Log Group Discovery

If `--log-group` is not specified, the tool lists all log groups with the `/aws/connect/` prefix:

- **One found** — used automatically
- **Multiple found** — displays a numbered list and prompts for a selection

## Query Files

Write standard CloudWatch Logs Insights query syntax in any `.sql` or `.txt` file. Example:

```sql
fields @timestamp, @message
| filter @message like /ContactId/
| sort @timestamp desc
| limit 200
```

Save the file anywhere and pass it with `--query`.

## Output

Results are written to an `.xlsx` file with:
- Styled header row (dark blue background, white bold text)
- Auto-sized column widths (capped at 60 characters)
- The `@ptr` field (internal CloudWatch pointer) is automatically excluded

The default filename includes a timestamp: `results_20260303_143012.xlsx`

## Required IAM Permissions

```
logs:DescribeLogGroups
logs:StartQuery
logs:GetQueryResults
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeLogGroups` | Discover `/aws/connect/` log groups |
| `StartQuery` | Submit the Logs Insights query |
| `GetQueryResults` | Poll for status and retrieve results |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: auto log group discovery, relative and absolute time ranges, Excel export with styled headers and auto-column widths |
