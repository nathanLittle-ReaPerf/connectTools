#!/usr/bin/env python3
"""flow-to-chart: Convert an exported Amazon Connect contact flow JSON to a flowchart.

Accepts files produced by export_flow.py (envelope format) or raw flow content JSON.
Outputs Mermaid (default), self-contained HTML, or Graphviz DOT.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# ── Action type metadata ──────────────────────────────────────────────────────

TYPE_LABELS = {
    "MessageParticipant":       "Play Message",
    "CheckAttribute":           "Check Attribute",
    "CheckContactAttributes":   "Check Attribute",
    "GetUserInput":             "Get Input",
    "SetQueue":                 "Set Queue",
    "TransferContactToQueue":   "Transfer to Queue",
    "DisconnectParticipant":    "Disconnect",
    "InvokeLambdaFunction":     "Lambda",
    "InvokeFlowModule":         "Invoke Flow",
    "SetAttributes":            "Set Attribute",
    "UpdateContactAttributes":  "Update Attribute",
    "CheckHoursOfOperation":    "Check Hours",
    "SetLoggingBehavior":       "Set Logging",
    "UpdateContactData":        "Update Contact",
    "SetRecordingBehavior":     "Set Recording",
    "CreateTask":               "Create Task",
    "SetContactFlow":           "Set Flow",
    "TransferContactToFlow":    "Transfer to Flow",
    "EndFlowExecution":         "End Flow",
    "HoldParticipantConnection": "Hold",
    "ResumeContactRecording":   "Resume Recording",
    "StopContactRecording":     "Stop Recording",
    "SuspendContactRecording":  "Suspend Recording",
    "StartMediaStreaming":      "Start Streaming",
    "StopMediaStreaming":       "Stop Streaming",
    "AssignContactCategory":    "Assign Category",
    "UpdateContactSchedule":    "Update Schedule",
    "SendNotification":         "Send Notification",
}

# Diamond shape in charts — actions that branch on a condition
DECISION_TYPES = {
    "CheckAttribute",
    "CheckContactAttributes",
    "GetUserInput",
    "CheckHoursOfOperation",
    "CheckStaffingStatus",
}

# Terminal shape — actions that end or hand off the contact
TERMINAL_TYPES = {
    "DisconnectParticipant",
    "TransferContactToQueue",
    "TransferContactToFlow",
    "EndFlowExecution",
}


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_graph(content):
    """
    Parse the Actions array into nodes and edges.

    Returns:
        nodes  — dict {id: {label, hint, type, is_decision, is_terminal}}
        edges  — list of {src, dst, label}
        start_id — ID of the first action
    """
    raw_actions = content.get("Actions") or []
    actions = {a["Identifier"]: a for a in raw_actions}
    start_id = content.get("StartAction")

    nodes = {}
    for aid, action in actions.items():
        atype = action.get("Type", "Unknown")
        nodes[aid] = {
            "label": TYPE_LABELS.get(atype, atype),
            "hint":  _param_hint(action),
            "type":  atype,
            "is_decision": atype in DECISION_TYPES,
            "is_terminal": atype in TERMINAL_TYPES,
        }

    edges = []
    for aid, action in actions.items():
        trans = action.get("Transitions") or {}

        # Default / success path
        next_id = trans.get("NextAction")
        if next_id and next_id in actions:
            edges.append({"src": aid, "dst": next_id, "label": ""})

        # Error branches
        for err in trans.get("Errors") or []:
            dst = err.get("NextAction")
            if dst and dst in actions:
                edges.append({"src": aid, "dst": dst, "label": _err_label(err.get("ErrorType", "Error"))})

        # Condition branches
        for cond in trans.get("Conditions") or []:
            dst = cond.get("NextAction")
            if dst and dst in actions:
                edges.append({"src": aid, "dst": dst, "label": _cond_label(cond.get("Condition") or {})})

    return nodes, edges, start_id


def _param_hint(action):
    """Pull the single most useful parameter value for a node label."""
    t = action.get("Type", "")
    p = action.get("Parameters") or {}

    extractors = {
        "MessageParticipant":     lambda: p.get("Text") or _tail(p.get("PromptId", "")),
        "CheckAttribute":         lambda: p.get("Attribute") or p.get("AttributeToCheck"),
        "CheckContactAttributes": lambda: p.get("Attribute") or p.get("AttributeToCheck"),
        "GetUserInput":           lambda: p.get("Text") or _tail(p.get("PromptId", "")),
        "SetQueue":               lambda: _tail(_nested(p, "Queue", "Id") or p.get("QueueId", "")),
        "InvokeLambdaFunction":   lambda: _tail(p.get("LambdaFunctionARN", "")),
        "InvokeFlowModule":       lambda: _tail(p.get("FlowModuleId", "")),
        "TransferContactToQueue": lambda: _tail(p.get("ContactFlowId", "")),
        "SetAttributes":          lambda: _attrs_summary(p.get("Attributes") or {}),
    }

    fn = extractors.get(t)
    if fn:
        try:
            result = fn()
            if result:
                return str(result)[:40]
        except Exception:
            pass
    return ""


def _tail(arn_or_id):
    """Last segment of an ARN or slash-delimited ID."""
    if not arn_or_id:
        return ""
    return str(arn_or_id).rstrip("/").split("/")[-1]


def _nested(d, *keys):
    """Safe nested dict access: _nested(p, 'Queue', 'Id')."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _attrs_summary(attrs):
    pairs = [f"{k}={v}" for k, v in list(attrs.items())[:2]]
    return ", ".join(pairs)


def _err_label(error_type):
    return (error_type
            .replace("NoMatchingError", "No Match")
            .replace("NoMatchingCondition", "No Match")
            .replace("Error", "Err"))


def _cond_label(condition):
    op = condition.get("Operator", "")
    operands = condition.get("Operands") or []
    val = ", ".join(str(o) for o in operands[:2])[:20]
    mapping = {"Equals": "=", "NotEquals": "!=", "Contains": "has",
               "StartsWith": "starts", "GreaterThan": "gt", "LessThan": "lt"}
    return f"{mapping.get(op, op)} {val}".strip()


# ── Shared helpers ────────────────────────────────────────────────────────────

def node_id(uid):
    """UUID or name → safe identifier for Mermaid/DOT (alphanumeric + underscore only)."""
    return "n" + re.sub(r"[^a-zA-Z0-9]", "_", uid)


def _safe_label(text):
    """Sanitize text for use in Mermaid and DOT labels."""
    return (text
            .replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            .replace("&", "and")
            .replace('"', "'")
            .replace("{", "(")
            .replace("}", ")")
            .replace("<", "lt ")
            .replace(">", "gt ")
            .strip())


# ── Mermaid renderer ──────────────────────────────────────────────────────────

def to_mermaid(nodes, edges, start_id, flow_name):
    lines = [
        "flowchart TD",
        f"  %% {flow_name}",
    ]

    # Virtual start node
    if start_id:
        lines.append('  START(["Start"])')
        lines.append(f"  START --> {node_id(start_id)}")
        lines.append("")

    # Nodes
    for uid, node in nodes.items():
        nid = node_id(uid)
        lbl = _safe_label(node["label"])
        if node["hint"]:
            lbl += f" | {_safe_label(node['hint'])}"
        if node["is_terminal"]:
            lines.append(f'  {nid}(["{lbl}"])')
        elif node["is_decision"]:
            lines.append(f'  {nid}{{"{lbl}"}}')
        else:
            lines.append(f'  {nid}["{lbl}"]')

    lines.append("")

    # Edges
    for edge in edges:
        src = node_id(edge["src"])
        dst = node_id(edge["dst"])
        lbl = _safe_label(edge["label"])
        if lbl:
            lines.append(f'  {src} -->|"{lbl}"| {dst}')
        else:
            lines.append(f"  {src} --> {dst}")

    return "\n".join(lines)


# ── HTML renderer (Cytoscape.js) ──────────────────────────────────────────────

def to_html(nodes, edges, start_id, flow_name):
    """Render the flow graph as a self-contained HTML file using Cytoscape.js.

    Cytoscape sizes nodes to fit their text, wraps long labels, and positions
    edge labels without overlap. Includes a slide-in color panel with presets
    and per-node-type color pickers.
    """
    elements = []

    if start_id:
        elements.append({"data": {"id": "START", "label": "Start", "ntype": "start"}})
        elements.append({"data": {
            "id": "e_start", "source": "START",
            "target": node_id(start_id), "label": "",
        }})

    for uid, node in nodes.items():
        nid   = node_id(uid)
        label = node["label"]
        if node["hint"]:
            label += "\n" + node["hint"]
        ntype = ("terminal" if node["is_terminal"]
                 else "decision" if node["is_decision"]
                 else "default")
        elements.append({"data": {"id": nid, "label": label, "ntype": ntype}})

    for i, edge in enumerate(edges):
        elements.append({"data": {
            "id": f"e{i}",
            "source": node_id(edge["src"]),
            "target": node_id(edge["dst"]),
            "label": edge["label"],
        }})

    elements_json = json.dumps(elements, ensure_ascii=False)
    safe_title    = (flow_name.replace("&", "&amp;")
                              .replace("<", "&lt;")
                              .replace(">", "&gt;"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{safe_title}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f0f2f5; height: 100vh; display: flex; flex-direction: column;
    }}

    /* ── Header ── */
    header {{
      padding: 8px 16px; background: white; border-bottom: 1px solid #ddd;
      display: flex; align-items: center; gap: 12px; flex-shrink: 0;
      position: relative; z-index: 10;
    }}
    h1 {{
      font-size: 1rem; color: #333; flex: 1;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    .controls {{ display: flex; gap: 6px; align-items: center; flex-shrink: 0; }}
    button {{
      padding: 4px 12px; border: 1px solid #ccc; border-radius: 4px;
      background: white; cursor: pointer; font-size: 0.9rem;
    }}
    button:hover {{ background: #f0f0f0; }}
    #btn-colors.active {{ background: #E3F2FD; border-color: #90CAF9; }}
    #zoom-label {{ font-size: 0.85rem; color: #666; min-width: 46px; text-align: center; }}

    /* ── Diagram canvas ── */
    #cy {{ flex: 1; }}

    /* ── Color panel ── */
    #color-panel {{
      position: fixed; right: -260px; top: 0; width: 248px; bottom: 0;
      background: white; border-left: 1px solid #ddd;
      box-shadow: -4px 0 16px rgba(0,0,0,0.1);
      transition: right 0.22s ease; z-index: 300;
      display: flex; flex-direction: column;
    }}
    #color-panel.open {{ right: 0; }}

    .panel-header {{
      padding: 10px 14px; font-weight: 600; font-size: 0.9rem;
      border-bottom: 1px solid #eee; display: flex;
      align-items: center; justify-content: space-between; flex-shrink: 0;
    }}
    .panel-header button {{
      padding: 2px 8px; font-size: 1rem; border: none;
      background: none; color: #888; cursor: pointer;
    }}
    .panel-header button:hover {{ background: #f5f5f5; color: #333; }}
    .panel-body {{ overflow-y: auto; flex: 1; padding: 12px 14px; }}

    .section-title {{
      font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
      color: #888; letter-spacing: 0.05em; margin: 12px 0 8px;
    }}
    .section-title:first-child {{ margin-top: 0; }}

    /* Preset theme buttons */
    .preset-grid {{
      display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 4px;
    }}
    .preset-btn {{
      padding: 6px 4px; font-size: 0.8rem; border-radius: 4px;
      border: 1px solid #ddd; background: white; cursor: pointer;
      display: flex; align-items: center; justify-content: center; gap: 6px;
    }}
    .preset-btn:hover {{ background: #f5f5f5; }}
    .preset-btn .dots {{
      display: flex; gap: 2px;
    }}
    .preset-btn .dot {{
      width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
    }}

    /* Color rows */
    .color-row {{
      display: flex; align-items: center; gap: 8px;
      padding: 5px 0; border-bottom: 1px solid #f5f5f5;
    }}
    .color-row:last-child {{ border-bottom: none; }}
    .swatch {{
      width: 20px; height: 20px; border-radius: 4px;
      border: 1px solid rgba(0,0,0,0.15); flex-shrink: 0;
    }}
    .color-row label {{ flex: 1; font-size: 0.85rem; color: #444; }}
    .color-row input[type="color"] {{
      width: 32px; height: 24px; padding: 1px; border: 1px solid #ccc;
      border-radius: 4px; cursor: pointer; background: white;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{safe_title}</h1>
    <div class="controls">
      <button id="btn-zoom-in">+</button>
      <span id="zoom-label">100%</span>
      <button id="btn-zoom-out">−</button>
      <button id="btn-reset">Fit</button>
      <button id="btn-colors">Colors</button>
    </div>
  </header>

  <div id="cy"></div>

  <!-- Slide-in color panel -->
  <div id="color-panel">
    <div class="panel-header">
      <span>Colors</span>
      <button id="btn-close-panel" title="Close">×</button>
    </div>
    <div class="panel-body">

      <div class="section-title">Presets</div>
      <div class="preset-grid">
        <button class="preset-btn" data-theme="default">
          <span class="dots">
            <span class="dot" style="background:#2E7D32"></span>
            <span class="dot" style="background:#E3F2FD"></span>
            <span class="dot" style="background:#1565C0"></span>
            <span class="dot" style="background:#B71C1C"></span>
          </span>Default
        </button>
        <button class="preset-btn" data-theme="dark">
          <span class="dots">
            <span class="dot" style="background:#1B5E20"></span>
            <span class="dot" style="background:#1A237E"></span>
            <span class="dot" style="background:#4527A0"></span>
            <span class="dot" style="background:#7F0000"></span>
          </span>Dark
        </button>
        <button class="preset-btn" data-theme="pastel">
          <span class="dots">
            <span class="dot" style="background:#81C784"></span>
            <span class="dot" style="background:#BBDEFB"></span>
            <span class="dot" style="background:#CE93D8"></span>
            <span class="dot" style="background:#EF9A9A"></span>
          </span>Pastel
        </button>
        <button class="preset-btn" data-theme="mono">
          <span class="dots">
            <span class="dot" style="background:#424242"></span>
            <span class="dot" style="background:#F5F5F5"></span>
            <span class="dot" style="background:#757575"></span>
            <span class="dot" style="background:#212121"></span>
          </span>Mono
        </button>
      </div>

      <div class="section-title">Node Colors</div>
      <div class="color-row">
        <div class="swatch" id="sw-start"></div>
        <label>Start</label>
        <input type="color" id="c-start">
      </div>
      <div class="color-row">
        <div class="swatch" id="sw-action"></div>
        <label>Action</label>
        <input type="color" id="c-action">
      </div>
      <div class="color-row">
        <div class="swatch" id="sw-decision"></div>
        <label>Decision</label>
        <input type="color" id="c-decision">
      </div>
      <div class="color-row">
        <div class="swatch" id="sw-terminal"></div>
        <label>Terminal</label>
        <input type="color" id="c-terminal">
      </div>

      <div class="section-title">Edge</div>
      <div class="color-row">
        <div class="swatch" id="sw-edge"></div>
        <label>Edges</label>
        <input type="color" id="c-edge">
      </div>

    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/cytoscape@3/dist/cytoscape.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dagre@0.8.5/dist/dagre.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
  <script>
    // ── Cytoscape init ────────────────────────────────────────────────────────
    const cy = cytoscape({{
      container: document.getElementById('cy'),
      elements:  {elements_json},
      wheelSensitivity: 0.3,
      style: [
        {{
          selector: 'node',
          style: {{
            'label': 'data(label)', 'text-valign': 'center', 'text-halign': 'center',
            'text-wrap': 'wrap', 'text-max-width': '160px',
            'font-size': '11px', 'font-family': 'Arial, sans-serif',
            'width': 'label', 'height': 'label', 'padding': '10px',
            'shape': 'roundrectangle', 'border-width': 1,
            'background-color': '#E3F2FD', 'border-color': '#90CAF9', 'color': '#333',
          }}
        }},
        {{
          selector: 'node[ntype="decision"]',
          style: {{
            'shape': 'diamond', 'text-max-width': '100px', 'padding': '20px',
            'background-color': '#1565C0', 'border-color': '#0D47A1', 'color': 'white',
          }}
        }},
        {{
          selector: 'node[ntype="terminal"]',
          style: {{
            'shape': 'ellipse',
            'background-color': '#B71C1C', 'border-color': '#7F0000', 'color': 'white',
          }}
        }},
        {{
          selector: 'node[ntype="start"]',
          style: {{
            'shape': 'ellipse', 'font-weight': 'bold',
            'background-color': '#2E7D32', 'border-color': '#1B5E20', 'color': 'white',
          }}
        }},
        {{
          selector: 'edge',
          style: {{
            'label': 'data(label)', 'font-size': '9px', 'font-family': 'Arial, sans-serif',
            'color': '#444', 'text-background-color': 'white',
            'text-background-opacity': 0.9, 'text-background-padding': '2px',
            'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
            'arrow-scale': 0.8, 'line-color': '#aaa', 'target-arrow-color': '#aaa',
            'width': 1.5,
          }}
        }}
      ],
      layout: {{
        name: 'dagre', rankDir: 'TB',
        nodeSep: 60, rankSep: 80, edgeSep: 20, padding: 40,
        animate: false, fit: true,
      }}
    }});

    // ── Zoom controls ─────────────────────────────────────────────────────────
    const fitZoom   = cy.zoom();
    const zoomLabel = document.getElementById('zoom-label');

    function updateLabel() {{
      zoomLabel.textContent = Math.round(cy.zoom() / fitZoom * 100) + '%';
    }}
    cy.on('zoom', updateLabel);
    updateLabel();

    function fit() {{ cy.fit(undefined, 40); updateLabel(); }}
    function zoomBy(f) {{
      cy.zoom({{ level: cy.zoom() * f, renderedPosition: {{ x: cy.width()/2, y: cy.height()/2 }} }});
    }}

    document.getElementById('btn-zoom-in') .addEventListener('click', () => zoomBy(1.25));
    document.getElementById('btn-zoom-out').addEventListener('click', () => zoomBy(0.8));
    document.getElementById('btn-reset')   .addEventListener('click', fit);
    window.addEventListener('resize', fit);

    // ── Color panel ───────────────────────────────────────────────────────────
    const THEMES = {{
      default: {{ start:'#2E7D32', action:'#E3F2FD', decision:'#1565C0', terminal:'#B71C1C', edge:'#aaaaaa' }},
      dark:    {{ start:'#1B5E20', action:'#1A237E', decision:'#4527A0', terminal:'#7F0000', edge:'#555555' }},
      pastel:  {{ start:'#81C784', action:'#BBDEFB', decision:'#CE93D8', terminal:'#EF9A9A', edge:'#BDBDBD' }},
      mono:    {{ start:'#424242', action:'#F5F5F5', decision:'#757575', terminal:'#212121', edge:'#9E9E9E' }},
    }};

    // Auto-pick black or white text based on background luminance
    function textColor(hex) {{
      const [r,g,b] = [1,3,5].map(i => parseInt(hex.slice(i,i+2),16));
      return (0.299*r + 0.587*g + 0.114*b) / 255 > 0.5 ? '#333333' : '#ffffff';
    }}
    // Darken a hex color by a fraction for use as border color
    function darken(hex, pct=0.25) {{
      return '#' + [1,3,5].map(i =>
        Math.round(Math.max(0, parseInt(hex.slice(i,i+2),16) * (1-pct)))
          .toString(16).padStart(2,'0')
      ).join('');
    }}

    function applyColors(c) {{
      cy.batch(() => {{
        cy.nodes('[ntype="start"]')    .style({{ 'background-color':c.start,    'border-color':darken(c.start),    'color':textColor(c.start)    }});
        cy.nodes('[ntype="default"]')  .style({{ 'background-color':c.action,   'border-color':darken(c.action),   'color':textColor(c.action)   }});
        cy.nodes('[ntype="decision"]') .style({{ 'background-color':c.decision, 'border-color':darken(c.decision), 'color':textColor(c.decision) }});
        cy.nodes('[ntype="terminal"]') .style({{ 'background-color':c.terminal, 'border-color':darken(c.terminal), 'color':textColor(c.terminal) }});
        cy.edges().style({{ 'line-color':c.edge, 'target-arrow-color':c.edge }});
      }});
      // Sync swatches and pickers
      [['start',c.start],['action',c.action],['decision',c.decision],['terminal',c.terminal],['edge',c.edge]]
        .forEach(([k,v]) => {{
          document.getElementById('sw-'+k).style.background = v;
          document.getElementById('c-'+k).value = v;
        }});
    }}

    let colors = {{ ...THEMES.default }};
    applyColors(colors);

    // Live-update on picker change
    ['start','action','decision','terminal','edge'].forEach(k => {{
      document.getElementById('c-'+k).addEventListener('input', e => {{
        colors[k] = e.target.value;
        applyColors(colors);
      }});
    }});

    // Preset buttons
    document.querySelectorAll('.preset-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        colors = {{ ...THEMES[btn.dataset.theme] }};
        applyColors(colors);
      }});
    }});

    // Toggle panel
    const panel    = document.getElementById('color-panel');
    const btnColors = document.getElementById('btn-colors');
    // Position panel below header
    const headerH  = document.querySelector('header').getBoundingClientRect().height;
    panel.style.top = headerH + 'px';

    function togglePanel() {{
      panel.classList.toggle('open');
      btnColors.classList.toggle('active');
    }}
    btnColors.addEventListener('click', togglePanel);
    document.getElementById('btn-close-panel').addEventListener('click', () => {{
      panel.classList.remove('open');
      btnColors.classList.remove('active');
    }});
  </script>
</body>
</html>"""


# ── Graphviz DOT renderer ─────────────────────────────────────────────────────

# Colour palette
_C = {
    "start":    ("#2E7D32", "white"),   # dark green
    "decision": ("#1565C0", "white"),   # dark blue
    "terminal": ("#B71C1C", "white"),   # dark red
    "default":  ("#E3F2FD", "#333"),    # light blue / dark text
}


def to_dot(nodes, edges, start_id, flow_name):
    safe_name = flow_name.replace('"', "'")
    lines = [
        f'digraph "{safe_name}" {{',
        "  rankdir=TD;",
        '  node [fontname="Arial" fontsize=11 margin="0.15,0.1"];',
        '  edge [fontname="Arial" fontsize=9];',
        "",
    ]

    # Virtual start
    if start_id:
        fg, bg = _C["start"]
        lines.append(
            f'  START [label="Start" shape=oval '
            f'style="filled" fillcolor="{fg}" fontcolor="{bg}"];'
        )
        lines.append(f"  START -> {node_id(start_id)};")
        lines.append("")

    # Nodes
    for uid, node in nodes.items():
        nid = node_id(uid)
        lbl = _safe_label(node["label"])
        if node["hint"]:
            lbl += f" | {_safe_label(node['hint'])}"

        if node["is_terminal"]:
            fg, bg = _C["terminal"]
            shape = "oval"
        elif node["is_decision"]:
            fg, bg = _C["decision"]
            shape = "diamond"
        else:
            fg, bg = _C["default"]
            shape = "rectangle"

        lines.append(
            f'  {nid} [label="{lbl}" shape={shape} '
            f'style="filled" fillcolor="{fg}" fontcolor="{bg}"];'
        )

    lines.append("")

    # Edges
    for edge in edges:
        src = node_id(edge["src"])
        dst = node_id(edge["dst"])
        lbl = _safe_label(edge["label"])
        attr = f' [label="{lbl}"]' if lbl else ""
        lines.append(f"  {src} -> {dst}{attr};")

    lines.append("}")
    return "\n".join(lines)


# ── Argument parsing ──────────────────────────────────────────────────────────

DEFAULT_EXT = {"mermaid": ".md", "html": ".html", "dot": ".dot"}


def parse_args():
    p = argparse.ArgumentParser(
        description="Convert an exported Amazon Connect contact flow JSON to a flowchart.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s Main_IVR.json                        # Mermaid  → Main_IVR.md
  %(prog)s Main_IVR.json --format html          # HTML     → Main_IVR.html
  %(prog)s Main_IVR.json --format dot           # DOT      → Main_IVR.dot
  %(prog)s Main_IVR.json --stdout               # print to stdout
  %(prog)s Main_IVR.json --output diagram.md    # explicit output path

  # Full pipeline: export then chart
  python export_flow.py  --instance-id <UUID> --name "Main IVR" --output Main_IVR.json
  python flow_to_chart.py Main_IVR.json --format html
        """,
    )
    p.add_argument("flow_file", metavar="FLOW_JSON",
                   help="Exported flow JSON (from export_flow.py, or raw flow content)")
    p.add_argument("--format", choices=["mermaid", "html", "dot"], default="mermaid",
                   help="Output format (default: mermaid)")

    out = p.add_mutually_exclusive_group()
    out.add_argument("--output", metavar="FILE", help="Output file path")
    out.add_argument("--stdout", action="store_true", help="Print to stdout")

    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    # Load file
    try:
        with open(args.flow_file, encoding="utf-8") as f:
            exported = json.load(f)
    except FileNotFoundError:
        print(f"Error: file not found: {args.flow_file}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {args.flow_file}: {e}", file=sys.stderr)
        sys.exit(1)

    # Accept both our envelope format {"metadata":..., "content":...} and raw flow JSON
    if "content" in exported and "Actions" in (exported.get("content") or {}):
        content   = exported["content"]
        flow_name = (exported.get("metadata") or {}).get("name") or Path(args.flow_file).stem
    elif "Actions" in exported:
        content   = exported
        flow_name = Path(args.flow_file).stem
    else:
        print(
            "Error: file does not look like a contact flow "
            "(no 'Actions' array found).",
            file=sys.stderr,
        )
        sys.exit(1)

    nodes, edges, start_id = build_graph(content)

    if not nodes:
        print("Warning: no actions found in flow.", file=sys.stderr)

    # Render
    if args.format == "html":
        output = to_html(nodes, edges, start_id, flow_name)
    elif args.format == "dot":
        output = to_dot(nodes, edges, start_id, flow_name)
    else:
        output = to_mermaid(nodes, edges, start_id, flow_name)

    # Write
    if args.stdout:
        print(output)
        return

    out_path = args.output or (
        re.sub(r"[^\w\-]", "_", flow_name).strip("_") + DEFAULT_EXT[args.format]
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"Generated {args.format} chart for '{flow_name}' → {out_path}")

    if args.format == "dot":
        stem = Path(out_path).stem
        print(f"  Render: dot -Tpng {out_path} -o {stem}.png")
        print(f"  Or SVG: dot -Tsvg {out_path} -o {stem}.svg")
    elif args.format == "mermaid":
        print("  View:   https://mermaid.live  (paste file contents)")
    elif args.format == "html":
        print("  Open the file in any browser (needs internet for Cytoscape CDN)")


if __name__ == "__main__":
    main()
