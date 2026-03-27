# flow_sim.py — Contact Flow Simulator

Simulates a contact's path through Amazon Connect flows without making a live call. Loads a scenario file, walks the flow graph block by block — evaluating conditions, applying mock Lambda responses, following sub-flow transfers — and produces a step trace and an HTML visualization of the path taken.

Requires a local flow cache. Run `flow_map.py` first to populate it.

---

## Usage

```bash
# Basic simulation
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario.json

# Interactive mode — prompts at unresolved decision points
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario.json --interactive

# Save interactive choices back to the scenario file
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario.json \
    --interactive --save-choices

# Custom HTML output path
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario.json \
    --html my_trace.html

# Skip HTML
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario.json --no-html

# JSON output (pipe-friendly)
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario.json --json

# Save text trace to file
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario.json \
    --output trace.txt
```

---

## Options

| Option | Description |
|---|---|
| `--instance-id UUID` | Connect instance UUID — locates the local flow cache. |
| `--flow NAME` | Starting flow name (case-insensitive substring match). |
| `--scenario FILE` | Scenario JSON file (from `flow_map.py` or `scenario_from_logs.py`). |
| `--interactive` | Prompt at unresolved decision points instead of taking the default branch. |
| `--save-choices` | With `--interactive`, write resolved choices back to the scenario file. |
| `--html FILE` | HTML visualization path. Default: `sim_<flow-name>.html` |
| `--no-html` | Skip HTML generation. |
| `--output FILE` | Save text trace to FILE. |
| `--json` | Print trace as JSON to stdout. |

---

## How it works

The simulator walks the flow graph starting from the entry block (`StartAction`) of the named flow:

1. **Attribute blocks** (`UpdateContactAttributes`) — applies SET operations to the simulated contact state.
2. **Decision blocks** (`Compare`, `CheckHoursOfOperation`, `CheckStaffing`) — evaluates conditions against the current simulated state and follows the matching branch.
3. **Lambda blocks** (`InvokeExternalResource`) — looks up the function in the scenario's `lambda_mocks`; applies the mocked `$.External.*` values and follows Success/Error branch.
4. **Input blocks** (`GetUserInput`) — looks up the block key in `dtmf_inputs` and follows the matching branch.
5. **Transfer blocks** (`TransferContactToFlow`) — follows into the sub-flow and continues. Up to **12 nested transfers** and **200 total steps** before the simulation stops.
6. **Terminal blocks** (`DisconnectParticipant`, `TransferContactToQueue`) — simulation ends; final queue is reported.

### Unresolved decisions

When the simulation reaches a decision point with no matching scenario data:
- Without `--interactive`: takes the first available branch (default or first condition), logs a warning to stderr.
- With `--interactive`: pauses and prompts you to choose. Use `--save-choices` to write the choice back to the scenario file for future runs.

### Expression resolution

The simulator resolves these expressions from the scenario state:

| Expression | Resolves to |
|---|---|
| `$.Attributes.<key>` | Contact attribute value |
| `$.External.<key>` | Last Lambda mock's returned value |
| `$.CustomerEndpoint.Address` | `call_parameters.ani` |
| `$.SystemEndpoint.Address` | `call_parameters.dnis` |

---

## Outputs

### Terminal step trace

```
Step  Flow                    Block                  Type             Branch      Action
   1  Main IVR                Start                  —                —           Entry
   2  Main IVR                Check Hours            Check Hours      In Hours    in_hours=True (mock)
   3  Main IVR                Check Staffing         Check Staffing   Staffed     staffed=True (mock)
   4  Main IVR                Main Menu              Get Input        1           dtmf="1" from scenario
   5  Auth Flow               Auth Lambda            Lambda           Success     mock result=Success
   6  Auth Flow               Check Auth             Check Attribute  verified    $.Attributes.authStatus = verified
   7  Main IVR                Set Queue              Set Queue        —           queue=Billing Support
   8  Main IVR                Transfer to Queue      Transfer         —           TERMINAL
```

### HTML visualization (`sim_<flow-name>.html`)

A split-panel view:
- **Left panel** — scrollable step trace with flow/block/type/branch/action columns. Click a row to highlight the corresponding node in the graph.
- **Right panel** — Cytoscape.js flow graph with a tab per flow. Visited nodes are highlighted green; unvisited nodes are dimmed. Steps link to nodes on click.

### JSON output (`--json`)

Structured trace with per-step data including flow name, block ID, block label, block type, branch taken, action description, and terminal/transfer flags. Final state includes resolved attributes and queue.

---

## Simulation limits

| Limit | Default | Notes |
|---|---|---|
| Max flow depth | 12 | `TransferContactToFlow` nesting |
| Max total steps | 200 | Prevents infinite loops |

If either limit is hit, the simulation stops with a warning and outputs whatever steps were completed.

---

## Prerequisites

- Run `flow_map.py --instance-id <UUID> --region <region>` first to populate the flow cache at `~/.connecttools/flows/<instance-id>/`.
- `flow_sim.py` makes **no AWS API calls** — it reads only from the local cache and the scenario file.
