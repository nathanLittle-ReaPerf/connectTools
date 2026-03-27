# flowSim — Amazon Connect Flow Simulator

A standalone toolset for testing Amazon Connect contact flows without a live call. Scan your instance to build a complete map of every attribute, Lambda, and decision point, fill in a scenario file with test data, then simulate a contact from entry to queue/disconnect — step by step.

---

## How the tools fit together

```
Instance                  flow_map.py               flow_sim.py
─────────────────────     ─────────────────────     ─────────────────────────────
ListContactFlows     →    Flow cache                scenario_<id>.json (template)
DescribeContactFlow  →    ~/.connecttools/flows/  →  Fill in values
                          ─────────────────────     ─────────────────────────────
                          HTML report               Simulate step by step
                          (attribute reference)  →  HTML visualization of path
```

For building scenarios from real contacts instead of filling in the template by hand, use `scenario_from_logs.py`:

```
export_flow_logs.py  →   flowSim/Logs/          →   scenario_from_logs.py   →   Scenarios/
(pulls CW logs            logs_YYYYMMDD.json          extracts real values        ready for
 by date range)                                        or builds archetypes        flow_sim.py
```

---

## Quickstart

### 1. Map your instance

```bash
python flow_map.py --instance-id <UUID> --region us-east-1
```

Outputs:
- `flow_map_<UUID>.html` — browsable reference of every attribute and Lambda across all flows
- `scenario_<UUID>.json` — scenario template pre-populated with discovered attributes, Lambda ARNs, and DTMF options

### 2. Fill in the scenario

Open `scenario_<UUID>.json` and set values:

```json
{
  "call_parameters": {
    "ani": "+15555550100",
    "channel": "VOICE",
    "simulated_time": "2025-03-26T14:00:00"
  },
  "attributes": {
    "customerType": "premium",
    "accountNumber": "1234567890"
  },
  "lambda_mocks": {
    "auth-function": {
      "result": "Success",
      "attributes": { "authStatus": "verified", "customerId": "C001" }
    }
  },
  "dtmf_inputs": {
    "Main IVR / Main Menu": { "value": "1" }
  },
  "hours_mocks": {
    "<hoo-id>": { "in_hours": true }
  },
  "staffing_mocks": {
    "<queue-id>": { "staffed": true }
  }
}
```

The `_attr_hints` block in the template shows valid values found in the flows — use those as a guide.

### 3. Simulate

```bash
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario_<UUID>.json
```

Outputs a step trace to the terminal and `sim_Main_IVR.html` — a split-panel view with the step trace on the left and the highlighted flow graph on the right.

### Alternative: build scenarios from real CloudWatch logs

```bash
# Step 1 — Export logs from your Connect instance
python export_flow_logs.py --instance-id <UUID> --region us-east-1
# Saves to flowSim/Logs/logs_<date>.json (default: yesterday, up to 100 contacts)

# Step 2 — Build scenarios from the export
python scenario_from_logs.py Logs/logs_<date>.json

# Merge all contacts into one representative scenario:
python scenario_from_logs.py contacts.json --merge

# Extract a specific contact:
python scenario_from_logs.py contacts.json --contact-id <UUID>

# Generate named archetype scenarios (Premium, No_Account, Auth_Failed, …)
# using your flow map cache to identify the decision attributes:
python scenario_from_logs.py contacts.json --archetypes --instance-id <UUID>
```

The archetype mode cross-references your real contact data against the decision points discovered by `flow_map.py` and groups contacts by their combined attribute profile. Each group becomes a named scenario file. A coverage report shows which known attribute/value combinations had no matching contacts — those are gaps worth hand-crafting.

---

## Tools

| Script | Purpose |
|---|---|
| [`flow_map.py`](flow_map.md) | Scan all flows; build attribute map and scenario template |
| [`export_flow_logs.py`](export_flow_logs.md) | Pull contact flow logs from CloudWatch by date range |
| [`flow_sim.py`](flow_sim.md) | Simulate a contact path using a scenario file |
| [`scenario_from_logs.py`](scenario_from_logs.md) | Build scenario files from real CloudWatch log exports |

---

## IAM permissions required

`flow_map.py` (only needed on first run or `--force-refresh`):
```
connect:ListContactFlows
connect:DescribeContactFlow
```

`export_flow_logs.py` additionally requires:
```
connect:DescribeInstance
logs:FilterLogEvents on /aws/connect/<instance-alias>
```

`flow_sim.py` and `scenario_from_logs.py` make **no AWS API calls** — they work entirely from local files.

---

## Local cache

Flow JSON is stored at `~/.connecttools/flows/<instance-id>/` and reused on subsequent runs. The cache is considered stale after **60 days** and is automatically refreshed on the next `flow_map.py` run. Use `--force-refresh` to refresh sooner.

---

## Scenario file reference

| Section | What it controls |
|---|---|
| `call_parameters` | ANI, DNIS, channel, simulated time |
| `attributes` | Contact attribute values at the start of the simulation |
| `lambda_mocks` | Mock result (`Success`/`Error`) and `$.External.*` values returned per Lambda |
| `dtmf_inputs` | Key pressed at each `GetUserInput` block, keyed as `"Flow / Block"` |
| `hours_mocks` | Whether each hours-of-operation check passes (`in_hours: true/false`) |
| `staffing_mocks` | Whether each queue staffing check passes (`staffed: true/false`) |

Keys starting with `_` (e.g. `_note`, `_attr_hints`) are comments — they are ignored by `flow_sim.py`.
