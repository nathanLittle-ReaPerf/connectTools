# flow_map.py — Attribute and Decision Map

Scans every contact flow in an Amazon Connect instance and builds a complete reference map of every attribute, Lambda function, DTMF input block, hours-of-operation check, and staffing check across all flows. Caches flow JSON locally so subsequent runs don't require AWS credentials.

---

## Usage

```bash
# First run — fetches all flows and saves to cache
python flow_map.py --instance-id <UUID> --region us-east-1

# Subsequent runs — uses cache, no AWS credentials needed
python flow_map.py --instance-id <UUID>

# Force re-fetch even if cache is fresh
python flow_map.py --instance-id <UUID> --region us-east-1 --force-refresh

# Save the full JSON map as well
python flow_map.py --instance-id <UUID> --map map.json

# Custom output paths
python flow_map.py --instance-id <UUID> --html report.html --scenario my_scenario.json

# Skip one of the default outputs
python flow_map.py --instance-id <UUID> --no-scenario
python flow_map.py --instance-id <UUID> --no-html

# Local dev with a named AWS profile
python flow_map.py --instance-id <UUID> --region us-east-1 --profile my-admin
```

---

## Options

| Option | Description |
|---|---|
| `--instance-id UUID` | Connect instance UUID. Required. |
| `--region REGION` | AWS region. Required only when fetching (cache missing or stale). |
| `--profile NAME` | AWS named profile for local development. |
| `--force-refresh` | Ignore cache and re-fetch all flows from the instance. |
| `--html FILE` | HTML report path. Default: `flow_map_<instance-id>.html` |
| `--scenario FILE` | Scenario template path. Default: `scenario_<instance-id>.json` |
| `--map FILE` | Save the full JSON map to FILE. |
| `--no-html` | Skip HTML generation. |
| `--no-scenario` | Skip scenario template generation. |

---

## Outputs

### HTML report (`flow_map_<UUID>.html`)

A self-contained, browsable reference page. No server required — open directly in a browser.

- **Attributes table** — every contact attribute found across all flows, with click-to-expand rows showing:
  - `SET` (green) — where and to what value each attribute is assigned
  - `CHK` (orange) — where it's evaluated in a condition, with the comparison values
  - `REF` (blue) — where its value is read (e.g. passed to a Lambda or used in a SET value)
- **Lambda functions table** — every unique Lambda ARN and the flows that invoke it

### Scenario template (`scenario_<UUID>.json`)

A JSON file pre-populated with all discovered attributes (empty values), Lambda ARNs, DTMF option lists, hours checks, and staffing checks — ready to fill in and pass to `flow_sim.py`. The `_attr_hints` block lists valid comparison values found in the flows as a reference.

See [README.md](README.md) for the scenario file format.

### Full JSON map (`--map FILE`)

The complete structured data behind both outputs above. Useful for scripting or building your own tooling on top.

---

## IAM permissions

Only required when fetching (first run or `--force-refresh`):

```
connect:ListContactFlows
connect:DescribeContactFlow
```

No AWS credentials needed for subsequent runs — the cache is used.

---

## Local cache

Flows are stored as individual JSON files at:
```
~/.connecttools/flows/<instance-id>/
  manifest.json          — fetch timestamp and flow count
  <flow-id>.json         — one file per flow (export envelope format)
```

The cache is considered stale after **60 days**. `flow_map.py` automatically re-fetches when stale. Use `--force-refresh` to force a refresh at any time.

---

## What is scanned

| Block type | What's extracted |
|---|---|
| `UpdateContactAttributes` | Attribute keys and values being SET; `$.Attributes.*` and `$.External.*` refs in values |
| `Compare` | Attribute being checked and the operand values it's compared against |
| `InvokeExternalResource` | Lambda ARN; `$.Attributes.*` refs passed as inputs |
| `GetUserInput` | DTMF option values |
| `CheckHoursOfOperation` | Hours-of-operation resource ID |
| `CheckStaffing` | Queue resource ID |
| `TransferContactToFlow` | Target flow ID (used to build the flow graph for `flow_sim.py`) |
| All other blocks | `$.Attributes.*` references in any parameter |
