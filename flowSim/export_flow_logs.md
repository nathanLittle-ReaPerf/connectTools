# export_flow_logs.py — Flow Log Exporter

Pulls Amazon Connect contact flow-execution events from CloudWatch Logs and saves them as a JSON file in `flowSim/Logs/`. The output file is in `filter-log-events` format and is immediately usable by `scenario_from_logs.py`.

## Usage

```bash
python export_flow_logs.py --instance-id <UUID> --region <REGION> [OPTIONS]
```

## Options

| Option | Description |
|---|---|
| `--instance-id UUID` | **(required)** Connect instance UUID |
| `--region REGION` | **(required)** AWS region (e.g. `us-east-1`) |
| `--profile NAME` | AWS named profile for local development |

### Time range (mutually exclusive; default is yesterday)

| Option | Description |
|---|---|
| `--yesterday` | Previous calendar day in UTC (midnight to midnight) |
| `--last-week` | Previous 7 calendar days in UTC |
| `--last DURATION` | Rolling window from now: `30m`, `4h`, `2d`, `1w` |
| `--start DATETIME` | Start of window — `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS` (UTC) |
| `--end DATETIME` | End of window (used with `--start`; default: now) |

### Output

| Option | Description |
|---|---|
| `--max N` | Stop after N unique contacts (default `100`; `0` = unlimited) |
| `--out-dir DIR` | Output directory (default: `flowSim/Logs/`) |
| `--output FILE` | Output filename (auto-generated from time range if omitted) |
| `--list` | Preview contacts found without writing a file |
| `--json` | Print raw events JSON to stdout |

## Output file name

Files are auto-named from the time window:

| Window | Filename |
|---|---|
| `--yesterday` | `logs_20260326_to_20260327.json` |
| `--last-week` | `logs_20260320_to_20260327.json` |
| `--last 4h` | `logs_20260327_to_20260327_1600.json` |
| `--start 2026-03-01` | `logs_20260301_to_20260327.json` |

## How many contacts do you need?

| Goal | Recommended |
|---|---|
| Spot-check a single path | `--max 1` with `--contact-id` in scenario_from_logs |
| Cover the most common paths | `--max 50` (default) |
| Build full archetype coverage | `--max 100`+ |
| Rare/error paths | Target a specific window when the error occurred, with `--max 10` |

A typical Connect instance generates 20–100 flow events per contact, so 100 contacts ≈ 1–2 CloudWatch API pages. Exports are fast.

## What the exporter stops at

The `--max` limit applies to **unique ContactIds**. Once that many contacts have been seen, the exporter stops paginating CloudWatch. Events already fetched for those contacts are written to the file; any contact that would have been the 101st is excluded entirely.

This means:
- Contacts near the cutoff may have slightly truncated journeys (events from later pages are not fetched).
- For scenario building this is acceptable — the scenario builder handles partial journeys.
- Use `--max 0` to fetch everything in the window (may be slow on busy instances).

## Examples

```bash
# Yesterday's logs (default)
python export_flow_logs.py --instance-id <UUID> --region us-east-1

# Last 4 hours, max 50 contacts
python export_flow_logs.py --instance-id <UUID> --region us-east-1 --last 4h --max 50

# Last week, all contacts
python export_flow_logs.py --instance-id <UUID> --region us-east-1 --last-week --max 0

# Specific date range
python export_flow_logs.py --instance-id <UUID> --region us-east-1 \
    --start 2026-03-01 --end 2026-03-08

# Preview without saving
python export_flow_logs.py --instance-id <UUID> --region us-east-1 --list

# Pipe to scenario builder directly
python export_flow_logs.py --instance-id <UUID> --region us-east-1 --json | \
    python scenario_from_logs.py /dev/stdin
```

## Full workflow

```bash
# 1. Map the instance (one-time — builds decision-attribute cache)
python flow_map.py --instance-id <UUID> --region us-east-1

# 2. Export yesterday's logs
python export_flow_logs.py --instance-id <UUID> --region us-east-1

# 3. Build named archetype scenarios from the export
python scenario_from_logs.py Logs/logs_20260326_to_20260327.json \
    --archetypes --instance-id <UUID>

# 4. Simulate a flow against one of the archetypes
python flow_sim.py --instance-id <UUID> \
    --flow "Main IVR" \
    --scenario Scenarios/archetype_01_Premium.json
```

Or run the whole thing interactively via the `flowsim` CLI.

## Required IAM

```
connect:DescribeInstance
logs:FilterLogEvents on /aws/connect/<instance-alias>
```
