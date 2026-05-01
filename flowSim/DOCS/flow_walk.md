# flow_walk.py — Interactive Step-by-Step Flow Walker

Walks an Amazon Connect contact flow block by block in the terminal. Prompts at every decision point: DTMF inputs, Lambda mock responses, hours-of-operation and staffing checks. At the end, saves all captured values as a scenario file for `flow_sim.py`.

```bash
# Basic walk
python flow_walk.py --instance-id <UUID> --flow "Main IVR" --region us-east-1

# Pre-set caller ANI and dialed number
python flow_walk.py --instance-id <UUID> --flow "Main IVR" --ani +15551234567 --dnis +18005550100

# Pre-set a contact attribute before the walk starts
python flow_walk.py --instance-id <UUID> --flow "Billing" --attr customer_type=premium

# Multiple pre-set attributes
python flow_walk.py --instance-id <UUID> --flow "Main IVR" --attr language=en --attr tier=gold
```

**Prerequisites:** Flow cache must exist for the instance. Build it with `flow_map.py`:
```bash
python flow_map.py --instance-id <UUID> --region us-east-1
```

**Interactive behavior at each block type:**

| Block type | What happens |
|---|---|
| Play Message | Text displayed; continues automatically |
| Set Attribute | Prompts for value (pre-pinned attrs show `[pinned]` and skip) |
| Check Attribute | Evaluates condition against current state; shows all tested conditions |
| Get Input (DTMF) | Prompts for digit; shows valid options |
| Check Hours | Prompts `In hours? [Y/n]` |
| Check Staffing | Prompts `Staffed? [Y/n]` |
| Check Metric Data | Prompts `Condition met? [Y/n]` |
| Lambda | Shows resolved input params; prompts for Success/Error and output attributes |
| Transfer to Flow | Recurses into the sub-flow automatically |
| Disconnect / Transfer to Queue | Ends walk |

**Baseline attribute prompt:**

Before the walk starts, optionally pre-pin attribute values. Attributes are grouped by source:
- **Read from incoming state** — referenced in the flow but not set here; must be pre-set if they influence branching
- **Set by this flow** — will be set during the walk; pre-pin only to override

**Lambda responses:**

On first encounter of a Lambda, flow_walk detects `$.External.*` attributes referenced downstream and suggests them as the output template. Cached responses are reused on subsequent invocations (with an override prompt).

**Flag for review:**

After Lambda, decision, and Set Attribute blocks, you can type a note to flag the block for review. Flags are saved to `flowSim/for_review/<instance-id>.json`.

**Saving a scenario:**

At the end of the walk, save all captured values (DTMF inputs, Lambda mocks, hours/staffing outcomes, attributes) as a scenario JSON file in `flowSim/Scenarios/<instance-id>/`. Load it with:
```bash
python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario <file>
```

**HTML visualization:**

After each walk, an HTML visualization of the path taken is automatically saved to `flowSim/Simulations/walk_<FlowName>.html`.

**Loop detection:**

When a loop is detected (revisiting a block), the walk pauses and asks how many more iterations to run before stopping.

**Key bindings during prompts:**
- Type `..` at any prompt to return to the main menu
- `Ctrl+C` to stop the walk early

**Options:**

| Flag | Description |
|---|---|
| `--instance-id UUID` | Connect instance UUID (flow cache must exist) |
| `--flow NAME` | Starting flow name (case-insensitive substring match) |
| `--ani NUMBER` | Caller phone number (prompted if omitted) |
| `--dnis NUMBER` | Dialed phone number (optional) |
| `--attr KEY=VALUE` | Pre-set a contact attribute (repeatable) |
