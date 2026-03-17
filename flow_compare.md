# flow_compare.py

Diff two exported Amazon Connect contact flow JSON files. Reports blocks added, removed, and modified between two versions — with per-field diffs of Parameters and Transitions. No AWS calls required.

## Dependencies

No pip install required. Works entirely on local files.

## Usage

```bash
python flow_compare.py LEFT.json RIGHT.json
```

| Argument | Description |
|---|---|
| `LEFT.json` | Older / baseline flow export |
| `RIGHT.json` | Newer / changed flow export |
| `--json` | Print results as JSON to stdout |

### Examples

```bash
# Compare two versions of a flow
python flow_compare.py Main_IVR_v1.json Main_IVR_v2.json

# JSON output
python flow_compare.py old.json new.json --json | jq '.modified[].changes'
```

## Output

```
  ────────────────────────────────────────────────────────────────────────
  FLOW COMPARE
  ────────────────────────────────────────────────────────────────────────
  Left :  Main_IVR_v1  ·  2026-03-10  ·  23 blocks
  Right:  Main_IVR_v2  ·  2026-03-17  ·  25 blocks

  Start action: unchanged  →  'Main Menu'

  2 added  ·  1 removed  ·  3 modified  ·  20 unchanged

  ── ADDED ────────────────────────────────────────────────────────────────
  +  "Retry Prompt"       (PlayPrompt)
  +  "Escalation Path"    (Transfer)

  ── REMOVED ──────────────────────────────────────────────────────────────
  -  "Old Intro"          (PlayPrompt)

  ── MODIFIED ─────────────────────────────────────────────────────────────
  ~  "Main Menu"  (GetUserInput)
       Parameters.Text
         < "Press 1 for Sales"
         > "Press 1 for Sales or 2 for Support"
       Transitions.Conditions[1].NextAction
         < "OldTarget"
         > "NewTarget"
```

**JSON keys:** `added`, `removed`, `modified` (with `changes` array of `{path, left, right}`), `summary`, `start_action_changed`.

## Key Behaviours

- **No AWS calls** — works entirely on local exported JSON files.
- **Accepts both formats** — the `export_flow.py` envelope (`{"metadata":..., "content":...}`) and raw flow content JSON.
- **Blocks matched by Identifier** — Connect block Identifiers can be human-readable names or UUIDs; both work.
- **Per-field diffs** — Parameters and Transitions are flattened to dotted paths (e.g. `Parameters.Text`, `Transitions.Conditions[0].NextAction`) for precise change reporting.
- **List fields compared by index** — reordering Conditions or Errors is reported as a modification.
- **Absent fields** shown as `[absent]` when a field exists in one version but not the other.
- **Type changes** reported first if a block's type was changed (rare but possible).

## Workflow

```bash
# Export two versions of a flow, then compare
python export_flow.py --instance-id <UUID> --name "Main IVR" --output main_ivr_before.json
# ... make changes in Connect console ...
python export_flow.py --instance-id <UUID> --name "Main IVR" --output main_ivr_after.json
python flow_compare.py main_ivr_before.json main_ivr_after.json
```

## Changelog

| Version | Change |
|---|---|
| Initial | Block-level diff with per-field Parameter and Transition changes; JSON output |
