# flowSim — Amazon Connect Flow Simulation Suite

Tools for simulating, replaying, and analyzing Amazon Connect contact flows without making live calls. Works entirely from cached flow definitions and CloudWatch log exports.

## Tools

| Script | What it does |
|---|---|
| `flow_map.py` | Fetches all flows from an instance and caches them locally — required before using flow_walk or flow_sim |
| `flow_walk.py` | Interactive step-by-step terminal walk through a flow — prompts at every branch, Lambda, and DTMF input |
| `flow_sim.py` | Batch simulator — runs a scenario file through a flow and outputs an HTML visualization of the path taken |
| `scenario_from_logs.py` | Builds scenario files from real CloudWatch flow logs — extracts Lambda responses, DTMF inputs, and attribute values |
| `export_flow_logs.py` | Exports Connect CloudWatch flow logs for a time window to a local file (input for `scenario_from_logs.py`) |

## Typical Workflow

### 1. Build the flow cache (one-time setup per instance)
```bash
python flow_map.py --instance-id <UUID> --region us-east-1
```
Fetches all contact flows and stores them at `~/.connecttools/flows/<UUID>/`. Refresh any time flows change.

### 2. Walk a flow interactively
```bash
python flow_walk.py --instance-id <UUID> --flow "Main IVR"
```
Steps through the flow block by block. At each decision point you choose the branch, mock Lambda responses, enter DTMF digits, etc. At the end, save the session as a scenario file for repeatable replays.

### 3. Build scenarios from real traffic (optional)
```bash
# Export logs from CloudWatch (or download via console)
python export_flow_logs.py --instance-id <UUID> --last 24h --region us-east-1

# Build scenario files from the exported logs
python scenario_from_logs.py flowSim/Logs/logs_20260501.json

# Or build named archetype scenarios using the flow decision map
python scenario_from_logs.py flowSim/Logs/logs_20260501.json --archetypes --instance-id <UUID>
```

### 4. Replay a scenario
```bash
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario flowSim/Scenarios/Premium.json
```
Runs the scenario non-interactively and opens an HTML visualization of the path taken.

## Output Directories

| Directory | Contents |
|---|---|
| `flowSim/FlowMaps/` | Cached flow manifests (from `flow_map.py`) |
| `flowSim/Scenarios/` | Scenario JSON files |
| `flowSim/Simulations/` | HTML path visualizations |
| `flowSim/Logs/` | Exported CloudWatch log files (input for `scenario_from_logs.py`) |
| `flowSim/for_review/` | Blocks flagged for review during interactive walks |

## Scenario File Format

Scenarios are plain JSON files. Key fields:

```json
{
  "_name": "Premium Customer",
  "call_parameters": {
    "ani": "+15551234567",
    "channel": "VOICE"
  },
  "attributes": {
    "customerTier": "premium",
    "language": "en"
  },
  "lambda_mocks": {
    "auth-lookup": {
      "result": "Success",
      "attributes": { "accountStatus": "active", "memberSince": "2020" }
    }
  },
  "dtmf_inputs": {
    "Main IVR / Main Menu": { "value": "1" }
  },
  "hours_mocks": {},
  "staffing_mocks": {}
}
```

Generate scenarios manually, via `flow_walk.py` (save at end of walk), or via `scenario_from_logs.py` from real contacts.

## Attribute Source Grouping

When `scenario_from_logs.py --summary` displays attributes, they are grouped by source:
- **Set by flow** — set directly via `UpdateContactAttributes` blocks
- **Returned by Lambda: \<function-name\>** — values that came from a Lambda's external result

This makes it clear which attributes you need to configure in `lambda_mocks` vs. `attributes` when building a scenario.

## Prerequisites

- Python 3.8+
- boto3 (pre-installed in CloudShell)
- No additional pip installs required

Flow cache must exist for `flow_walk.py` and `flow_sim.py`. Run `flow_map.py` first.
