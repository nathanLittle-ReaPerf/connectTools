# cid_journey.py

Render an interactive Cytoscape.js caller journey map from a CID_Search output Excel file (produced by a CloudWatch Logs Insights query exported via `log_insights.py`). Each row in the xlsx becomes a node showing the flow block type and name. Nodes are colour-coded by block role (start, decision, Lambda invoke, terminal, default action), repeated nodes are highlighted with a dashed border, and clicking any node opens a side panel showing the raw log row details. No AWS calls are made — processing is entirely local.

## Dependencies

```bash
pip install openpyxl --user
```

The generated HTML file requires an internet connection in the browser to load Cytoscape.js and the dagre layout plugin from CDN.

## Usage

```bash
python cid_journey.py XLSX_FILE [--output FILE]
```

| Flag | Description |
|---|---|
| `XLSX_FILE` | Path to the CID_Search output xlsx file (required, positional) |
| `--output` | Output HTML file path — defaults to `<input_stem>_journey.html` in the same directory |

### Examples

```bash
# Generate journey map with default output path
python cid_journey.py CID-abc123_2026-03-04.xlsx

# Write to a specific file
python cid_journey.py CID-abc123_2026-03-04.xlsx --output journey.html
```

## Output

A self-contained HTML file that opens in any browser. The page shows:

- A **top-down DAG** (directed acyclic graph) of all flow blocks in sequence, laid out with dagre.
- **Node shapes and colours** by block type — green oval (start), blue diamond (decision), orange rectangle (Lambda), red oval (terminal), light-blue rectangle (default action). Repeated nodes (same flow + block type appearing more than once) get a dashed border.
- **Edge labels** showing the branch result from the preceding decision block (`Results` column).
- **Detail panel** (left slide-in) — click any node or the *Details* button to see all log columns for that row, including `@timestamp`, `ContactFlowName`, `ContactFlowModuleType`, `Attribute`, `Value`, `Check`, `Results`, `Prompt`, `Function`, and a computed `_delta_from_prev` timing field.
- **Colors panel** (right slide-in) — four preset themes (Default, Dark, Pastel, Mono) and per-node-type colour pickers with automatic text contrast adjustment.
- **Zoom controls** — +/−/Fit buttons and a zoom-percentage label.

A warning is printed if the xlsx contains more than 500 rows, as large maps may render slowly.

## Key Behaviours

- Expected xlsx columns: `@timestamp`, `ContactId`, `ContactFlowName`, `ContactFlowModuleType`, `Attribute`, `Value`, `Check`, `Results`, `Prompt`, `Operation`, `Function`, `Parameters`, `External_Results`. Missing columns generate a warning but do not stop processing.
- Timing deltas between consecutive flow blocks are computed from `@timestamp` values and injected as `_delta_from_prev` (e.g. `3s`) into the node detail panel.
- Edge labels come from the `Results` column of decision blocks only (`CheckAttribute`, `GetUserInput`, `StoreCustomerInput`, `CheckHoursOfOperation`, `CheckAgentStatus`, `WaitForCustomerInput`).
- The contact ID displayed in the page title is taken from the first row's `ContactId` column, falling back to the filename stem.
- The HTML uses CDN-hosted Cytoscape.js 3.x and cytoscape-dagre 2.5.0 — an internet connection is required when opening the file.

## Required IAM Permissions

```
None — operates on local files only.
```

## APIs Used

| API | Purpose |
|---|---|
| *(none)* | No AWS API calls are made; all processing is local |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: xlsx parsing, Cytoscape.js HTML generation, colour panel with presets, node detail side panel, timing delta computation |
