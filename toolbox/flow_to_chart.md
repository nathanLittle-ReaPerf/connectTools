# flow_to_chart.py

Convert an exported Amazon Connect contact flow JSON into a flowchart.

## Dependencies

No pip install required beyond the Python standard library. The HTML output loads Cytoscape.js and Dagre from CDN — an internet connection is needed to open those files in a browser.

## Usage

```bash
python flow_to_chart.py <FLOW_JSON> [options]
```

| Flag | Description |
|---|---|
| `FLOW_JSON` | Path to JSON file from `export_flow.py` (required) |
| `--format` | Output format: `mermaid` (default), `html`, or `dot` |
| `--output` | Write to a specific file path |
| `--stdout` | Print to stdout instead of writing a file |

### Default output filenames

| Format | Extension | Default name |
|---|---|---|
| `mermaid` | `.md` | `<Flow Name>.md` |
| `html` | `.html` | `<Flow Name>.html` |
| `dot` | `.dot` | `<Flow Name>.dot` |

### Examples

```bash
# Export a flow, then chart it
python export_flow.py --instance-id <UUID> --name "Main IVR" --output Main_IVR.json
python flow_to_chart.py Main_IVR.json --format html

# Mermaid to stdout (pipe-friendly)
python flow_to_chart.py Main_IVR.json --stdout

# Graphviz DOT, then render to PNG
python flow_to_chart.py Main_IVR.json --format dot
dot -Tpng Main_IVR.dot -o Main_IVR.png
```

## Input Formats

Accepts both:
- **Envelope format** — JSON produced by `export_flow.py` (`{"metadata": {...}, "content": {...}}`)
- **Raw flow content** — JSON with a top-level `"Actions"` array (copied directly from the AWS console)

## Output Formats

### HTML (recommended)

Self-contained interactive flowchart rendered with Cytoscape.js and the Dagre layout engine. Open in any browser.

- Nodes auto-size to fit their text, with multi-line wrapping for long labels
- Edge labels sit inline on connections without overlapping nodes
- **Zoom controls** — `+` / `−` buttons, scroll wheel, or drag to pan; `Fit` resets to the full-graph view
- **Zoom label** — shows percentage relative to the fit-to-screen level (fit = 100%)
- **Colors panel** — click the `Colors` button to open a slide-in panel with:
  - 4 preset themes: Default, Dark, Pastel, Mono
  - Individual color pickers for Start, Action, Decision, Terminal node types, and Edges
  - Text color auto-adjusts (black or white) based on background luminance

> Requires an internet connection to load Cytoscape.js and Dagre from CDN.

### Mermaid

Plain-text flowchart in Mermaid syntax. Paste into [mermaid.live](https://mermaid.live) or embed in any Markdown file that renders Mermaid (GitHub, Notion, Obsidian, etc.).

### DOT (Graphviz)

Standard DOT format. Render locally with Graphviz:

```bash
dot -Tpng flow.dot -o flow.png
dot -Tsvg flow.dot -o flow.svg
```

## Node Shapes

| Shape | Meaning |
|---|---|
| Green oval | Start of the flow |
| Rectangle / roundrect | Action (plays message, sets attribute, invokes Lambda, etc.) |
| Diamond | Decision (check attribute, check hours, get user input, etc.) |
| Red oval | Terminal (disconnect, transfer to queue/flow, end flow) |

## Required IAM Permissions

None — this tool reads local JSON files only. See `export_flow.py` for the permissions needed to download flows from AWS.

## Changelog

| Version | Change |
|---|---|
| Initial | Mermaid-only output; basic node/edge parsing from `Actions` array |
| v2 | Added DOT (Graphviz) format |
| v3 | Added HTML format; initial renderer used embedded Mermaid |
| v4 | Fixed Mermaid syntax errors: removed `▶` from Start node label; replaced `\n` in node labels with ` \| ` separator |
| v5 | Fixed `node_id()` to replace all non-alphanumeric characters (not just hyphens) — required for flows that use human-readable block names instead of UUIDs |
| v6 | Replaced HTML renderer with Cytoscape.js + Dagre layout for proper auto-sizing, text wrapping, and edge label placement |
| v7 | Added zoom/pan controls: `+` / `−` / `Fit` buttons, scroll-wheel zoom, drag to pan |
| v8 | Zoom label shows percentage relative to fit-to-screen level (fit = 100%) |
| v9 | Added slide-in Colors panel: 4 preset themes (Default/Dark/Pastel/Mono) and per-node-type color pickers with auto text contrast |
