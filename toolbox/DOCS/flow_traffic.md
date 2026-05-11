# flow_traffic.py — Flow Entry Counts and Contact Paths

Reads the Connect CloudWatch flow-execution log group to show two complementary views:

- **Flow Counts** — how many times each flow was entered (entries + unique contacts). Re-entries are counted separately, so if a contact transfers back to a flow it already visited, that's a second entry.
- **Contact Paths** — for each contact in the window, the ordered sequence of flows traversed. Each flow appears once per entry in the sequence.

```bash
# Last 24h (default)
python flow_traffic.py --instance-id <UUID> --region us-east-1

# Last 7 days
python flow_traffic.py --instance-id <UUID> --last 7d

# Filter to contacts that touched a specific flow
python flow_traffic.py --instance-id <UUID> --flow "Billing IVR"

# Single contact
python flow_traffic.py --instance-id <UUID> --contact-id <UUID>

# Counts only — no per-contact paths
python flow_traffic.py --instance-id <UUID> --no-paths

# Export contact paths to CSV
python flow_traffic.py --instance-id <UUID> --csv paths.csv

# JSON — pipe to jq
python flow_traffic.py --instance-id <UUID> --json | jq '.counts[:5]'
```

**APIs used:** `DescribeInstance`, `FilterLogEvents` (Connect flow log group)

**Required IAM:**
- `connect:DescribeInstance`
- `logs:FilterLogEvents` on `/aws/connect/<instance-alias>`

**Options:**

| Flag | Description |
|---|---|
| `--instance-id UUID` | Connect instance UUID |
| `--region REGION` | AWS region |
| `--flow NAME` | Filter to contacts that touched this flow (case-insensitive substring) |
| `--contact-id UUID` | Show counts and path for a single contact |
| `--last DURATION` | Relative window ending now (`1h`, `4h`, `7d`). Default: `24h` |
| `--start YYYY-MM-DD[THH:MM:SS]` | Absolute window start (UTC) |
| `--end YYYY-MM-DD[THH:MM:SS]` | Absolute window end (UTC). Default: now |
| `--max N` | Stop after N unique contacts (default 200; `0` for no limit) |
| `--no-paths` | Print flow counts only; omit the per-contact paths table |
| `--csv FILE` | Write contact paths to CSV (`contact_id`, `start_time`, `flow_count`, `path`) |
| `--json` | Print all results as JSON to stdout |
| `--output FILE` | Write JSON to a file (implies `--json`) |

**Key behaviors:**
- `--flow` filter applies to both FLOW COUNTS and CONTACT PATHS — narrows to contacts that touched that flow at any point
- `--contact-id` skips the time window prompt in toolbox mode
- Re-entries tracked by detecting ContactFlowId changes within each contact's event stream
- CSV output saved under `~/.connecttools/FlowTraffic/` unless an absolute path is given
