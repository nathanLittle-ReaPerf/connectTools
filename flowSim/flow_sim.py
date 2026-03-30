#!/usr/bin/env python3
"""flow_sim.py — Simulate a contact's path through Amazon Connect flows.

Loads a scenario file (produced by flow_map.py) and a starting flow name,
then walks the flow graph block by block — evaluating conditions, applying
mock Lambda responses, and following sub-flow transfers — until the contact
reaches a queue or disconnect. Outputs a step trace and an HTML visualization
with the taken path highlighted on the full flow graph.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CACHE_BASE      = Path.home() / ".connecttools" / "flows"
FLOWSIM_DIR     = Path(__file__).parent
SIMULATIONS_DIR = FLOWSIM_DIR / "Simulations"

_MAN = """\
NAME
    flow_sim.py — Simulate a contact's path through Amazon Connect flows

SYNOPSIS
    python flow_sim.py --instance-id UUID --flow NAME --scenario FILE [OPTIONS]

DESCRIPTION
    Walks a contact flow from start to queue/disconnect using mock data from
    a scenario file. Follows TransferContactToFlow blocks into sub-flows.
    Evaluates conditions, applies Lambda mock responses, and uses the
    scenario's DTMF inputs to choose menu branches.

    Scenario files are produced by flow_map.py. Run flow_map.py first to
    generate the template, fill it in, then run flow_sim.py.

OPTIONS
    --instance-id UUID
        Connect instance UUID — used to locate the local flow cache.

    --flow NAME
        Starting flow name (case-insensitive substring match).

    --scenario FILE
        Scenario JSON file (from flow_map.py).

    --interactive
        At unresolved decision points (no scenario data), prompt for input
        instead of taking the default branch.

    --save-choices
        When used with --interactive, write resolved choices back to the
        scenario file.

    --html FILE
        Path for the HTML visualization. Default: sim_<flow-name>.html

    --no-html
        Skip HTML generation.

    --output FILE
        Save text trace to FILE.

    --json
        Print trace as JSON to stdout.

EXAMPLES
    # First run: generate scenario template
    python flow_map.py --instance-id <UUID> --region us-east-1

    # Fill in scenario_<UUID>.json, then simulate
    python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario_<UUID>.json

    # Interactive mode — prompts at unresolved forks
    python flow_sim.py --instance-id <UUID> --flow "Main IVR" --scenario scenario.json --interactive

NOTES
    Requires a local flow cache. Run flow_map.py first to populate it.
    The cache lives at ~/.connecttools/flows/<instance-id>/.
"""


# ── Block type metadata (from flow_to_chart.py) ───────────────────────────────

TYPE_LABELS: dict[str, str] = {
    "MessageParticipant":        "Play Message",
    "CheckAttribute":            "Check Attribute",
    "CheckContactAttributes":    "Check Attribute",
    "Compare":                   "Check Attribute",
    "GetUserInput":              "Get Input",
    "SetQueue":                  "Set Queue",
    "TransferContactToQueue":    "Transfer to Queue",
    "DisconnectParticipant":     "Disconnect",
    "InvokeLambdaFunction":      "Lambda",
    "InvokeExternalResource":    "Lambda",
    "InvokeFlowModule":          "Invoke Flow",
    "SetAttributes":             "Set Attribute",
    "UpdateContactAttributes":   "Update Attribute",
    "CheckHoursOfOperation":     "Check Hours",
    "CheckStaffing":             "Check Staffing",
    "CheckStaffingStatus":       "Check Staffing",
    "SetLoggingBehavior":        "Set Logging",
    "SetRecordingBehavior":      "Set Recording",
    "SetVoice":                  "Set Voice",
    "TransferContactToFlow":     "Transfer to Flow",
    "TransferToFlow":            "Transfer to Flow",
    "EndFlowExecution":          "End Flow",
    "SetContactFlow":            "Set Flow",
    "HoldParticipantConnection": "Hold",
    "CreateTask":                "Create Task",
    "SendNotification":          "Send Notification",
}

DECISION_TYPES = {
    "CheckAttribute", "CheckContactAttributes", "Compare",
    "GetUserInput", "CheckHoursOfOperation",
    "CheckStaffing", "CheckStaffingStatus",
}

TERMINAL_TYPES = {
    "DisconnectParticipant",
    "TransferContactToQueue",
    "EndFlowExecution",
}

LAMBDA_TYPES    = {"InvokeLambdaFunction", "InvokeExternalResource"}
SET_ATTR_TYPES  = {"UpdateContactAttributes", "SetAttributes"}
TRANSFER_TYPES  = {"TransferContactToFlow", "TransferToFlow", "InvokeFlowModule"}


# ── Shared helpers ─────────────────────────────────────────────────────────────

def node_id(uid: str) -> str:
    return "n" + re.sub(r"[^a-zA-Z0-9]", "_", uid)


def _block_label(action: dict) -> str:
    ident = action.get("Identifier", "?")
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}", ident.lower()):
        return ident[:8] + "…"
    return ident[:60]


def _cond_label(condition: dict) -> str:
    op       = condition.get("Operator", "")
    operands = condition.get("Operands") or []
    val      = ", ".join(str(o) for o in operands[:2])[:20]
    mapping  = {
        "Equals": "=", "NotEquals": "!=", "Contains": "has",
        "StartsWith": "starts", "GreaterThan": ">", "LessThan": "<",
    }
    return f"{mapping.get(op, op)} {val}".strip()


def _he(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ── Cache loading ──────────────────────────────────────────────────────────────

def load_flow_cache(instance_id: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Returns (by_id, by_name_lower) — two indexes into the cached envelopes."""
    d = CACHE_BASE / instance_id
    if not d.exists():
        return {}, {}
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for p in d.glob("*.json"):
        if p.name == "manifest.json":
            continue
        try:
            env   = json.loads(p.read_text())
            meta  = env.get("metadata") or {}
            fid   = meta.get("id", "")
            fname = meta.get("name", "")
            if fid:
                by_id[fid] = env
            if fname:
                by_name[fname.lower()] = env
        except (json.JSONDecodeError, OSError):
            pass
    return by_id, by_name


def find_flow(name_or_id: str, by_id: dict, by_name: dict) -> dict | None:
    if name_or_id in by_id:
        return by_id[name_or_id]
    lower = name_or_id.lower()
    if lower in by_name:
        return by_name[lower]
    # Substring match
    matches = [env for key, env in by_name.items() if lower in key]
    if len(matches) == 1:
        return matches[0]
    return None


# ── Simulation state ───────────────────────────────────────────────────────────

@dataclass
class SimState:
    attributes:    dict = field(default_factory=dict)  # $.Attributes.*
    external:      dict = field(default_factory=dict)  # $.External.* (Lambda outputs)
    queue:         str  = ""
    contact_params: dict = field(default_factory=dict)  # ani, dnis, channel, etc.


@dataclass
class Step:
    flow_id:       str
    flow_name:     str
    block_id:      str
    block_label:   str
    block_type:    str
    type_label:    str
    action_desc:   str
    branch:        str
    next_block_id: str   = ""
    terminal:      bool  = False
    is_transfer:   bool  = False
    transfer_target: str = ""


# ── Expression resolution ──────────────────────────────────────────────────────

_ATTR_RE = re.compile(r'\$\.Attributes\.([a-zA-Z0-9_]+)')
_EXT_RE  = re.compile(r'\$\.External\.([a-zA-Z0-9_]+)')

def resolve(expr, state: SimState) -> str:
    if expr is None:
        return ""
    s = str(expr)
    s = _ATTR_RE.sub(lambda m: state.attributes.get(m.group(1), ""), s)
    s = _EXT_RE.sub( lambda m: state.external.get(m.group(1), ""), s)
    s = s.replace("$.CustomerEndpoint.Address", state.contact_params.get("ani") or "")
    s = s.replace("$.SystemEndpoint.Address",   state.contact_params.get("dnis") or "")
    return s


# ── Condition evaluation ───────────────────────────────────────────────────────

def evaluate(value: str, operator: str, operands: list[str]) -> bool:
    if not operands:
        return False
    v = str(value)
    if operator == "Equals":
        return v in operands
    if operator == "NotEquals":
        return v not in operands
    if operator == "Contains":
        return any(op in v for op in operands)
    if operator == "StartsWith":
        return any(v.startswith(op) for op in operands)
    if operator == "EndsWith":
        return any(v.endswith(op) for op in operands)
    _NUM_OPS = {
        "GreaterThan":                lambda a, b: a > b,
        "GreaterThanOrEqualTo":       lambda a, b: a >= b,
        "LessThan":                   lambda a, b: a < b,
        "LessThanOrEqualTo":          lambda a, b: a <= b,
        "NumberGreaterThan":          lambda a, b: a > b,
        "NumberGreaterThanOrEqualTo": lambda a, b: a >= b,
        "NumberGreaterOrEqualTo":     lambda a, b: a >= b,
        "NumberLessThan":             lambda a, b: a < b,
        "NumberLessThanOrEqualTo":    lambda a, b: a <= b,
        "NumberLessOrEqualTo":        lambda a, b: a <= b,
    }
    if operator in _NUM_OPS:
        try:
            return _NUM_OPS[operator](float(v), float(operands[0]))
        except (ValueError, IndexError):
            return False
    return False


# ── Interactive prompts ────────────────────────────────────────────────────────

def prompt_choice(label: str, options: list[str]) -> str:
    opts = "/".join(options) if options else "free input"
    while True:
        try:
            val = input(f"  [?] {label} [{opts}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return options[0] if options else ""
        if not options or val in options:
            return val
        print(f"      Invalid — choose from: {opts}")


def prompt_bool(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        val = input(f"  [?] {label} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not val:
        return default
    return val in ("y", "yes", "1", "true")


# ── Block execution ────────────────────────────────────────────────────────────

def execute_block(
    action: dict,
    flow_name: str,
    state: SimState,
    scenario: dict,
    interactive: bool,
) -> tuple[str, str, str, bool, str]:
    """
    Execute one block.
    Returns: (next_block_id, branch_label, action_desc, is_terminal, transfer_target)
    transfer_target is a flow ID/name when the block is a flow transfer.
    """
    btype  = action.get("Type", "")
    blabel = _block_label(action)
    params = action.get("Parameters") or {}
    trans  = action.get("Transitions") or {}

    default_next = trans.get("NextAction", "")
    errors       = trans.get("Errors") or []
    conditions   = trans.get("Conditions") or []
    error_next   = errors[0].get("NextAction", "") if errors else ""

    # ── Terminal ──────────────────────────────────────────────────────────────
    if btype in TERMINAL_TYPES:
        if btype == "TransferContactToQueue":
            q = (params.get("QueueId") or
                 (params.get("Queue") or {}).get("Id") or
                 state.queue or "configured queue")
            return "", "", f"→ Queue: {q}", True, ""
        if btype == "DisconnectParticipant":
            return "", "", "Contact disconnected", True, ""
        return "", "", "Flow execution ended", True, ""

    # ── Flow transfer ─────────────────────────────────────────────────────────
    if btype in TRANSFER_TYPES:
        target = (params.get("ContactFlowId") or
                  params.get("FlowModuleId") or "")
        return "", "→ sub-flow", f"Transfer to flow: {target[:20] or '?'}", False, target

    # ── Set attributes ────────────────────────────────────────────────────────
    if btype in SET_ATTR_TYPES:
        attrs = params.get("Attributes") or {}
        parts = []
        for key, val in attrs.items():
            resolved = resolve(val, state)
            state.attributes[key] = resolved
            parts.append(f"{key} = '{resolved}'")
        return default_next, "", "SET " + (", ".join(parts) or "(none)"), False, ""

    # ── Set queue ─────────────────────────────────────────────────────────────
    if btype == "SetQueue":
        q_id = (params.get("QueueId") or
                (params.get("Queue") or {}).get("Id") or "")
        state.queue = q_id
        return default_next, "", f"Queue → {q_id or '?'}", False, ""

    # ── Check attribute / Compare ─────────────────────────────────────────────
    if btype in ("CheckAttribute", "CheckContactAttributes", "Compare"):
        cmp_expr = (params.get("ComparisonValue") or
                    params.get("Attribute") or
                    params.get("AttributeToCheck") or "")
        resolved = resolve(cmp_expr, state)
        for cond in conditions:
            c = cond.get("Condition") or {}
            operands = [str(o) for o in (c.get("Operands") or [])]
            if evaluate(resolved, c.get("Operator", "Equals"), operands):
                label = _cond_label(c)
                return (cond.get("NextAction", ""), label,
                        f"'{cmp_expr}' = '{resolved}' → {label}", False, "")
        return default_next, "no match", f"'{cmp_expr}' = '{resolved}' → no match", False, ""

    # ── Check hours of operation ──────────────────────────────────────────────
    if btype == "CheckHoursOfOperation":
        hoo_id = params.get("HoursOfOperationId") or ""
        mock   = scenario.get("hours_mocks", {}).get(hoo_id)
        if mock is None:
            if interactive:
                in_hours = prompt_bool(f"Hours of operation [{hoo_id[:12] or '?'}]: In hours?", default=True)
            else:
                in_hours = True
                print(f"  [auto] Hours of operation {hoo_id[:12] or '?'}: assuming In Hours", file=sys.stderr)
        else:
            in_hours = mock.get("in_hours", True) if isinstance(mock, dict) else bool(mock)

        if in_hours:
            # In-hours: try Conditions for True/InHours, fall back to NextAction
            for cond in conditions:
                c = cond.get("Condition") or {}
                ops = [str(o).lower() for o in (c.get("Operands") or [])]
                if any(o in ("true", "inhours") for o in ops):
                    return cond.get("NextAction", default_next), "In Hours", "In Hours", False, ""
            return default_next, "In Hours", "In Hours", False, ""
        else:
            return error_next or default_next, "Out of Hours", "Out of Hours", False, ""

    # ── Check staffing ────────────────────────────────────────────────────────
    if btype in ("CheckStaffing", "CheckStaffingStatus"):
        q_id = params.get("QueueId") or ""
        mock = scenario.get("staffing_mocks", {}).get(q_id)
        if mock is None:
            if interactive:
                staffed = prompt_bool(f"Queue [{q_id[:12] or '?'}]: Staffed?", default=True)
            else:
                staffed = True
                print(f"  [auto] Queue {q_id[:12] or '?'}: assuming Staffed", file=sys.stderr)
        else:
            staffed = mock.get("staffed", True) if isinstance(mock, dict) else bool(mock)

        if staffed:
            for cond in conditions:
                c = cond.get("Condition") or {}
                ops = [str(o).lower() for o in (c.get("Operands") or [])]
                if any(o in ("true", "staffed") for o in ops):
                    return cond.get("NextAction", default_next), "Staffed", "Staffed", False, ""
            return default_next, "Staffed", "Staffed", False, ""
        else:
            return error_next or default_next, "Not Staffed", "Not Staffed", False, ""

    # ── Lambda ────────────────────────────────────────────────────────────────
    if btype in LAMBDA_TYPES:
        arn     = (params.get("LambdaFunctionARN") or params.get("FunctionArn") or
                   params.get("ResourceId") or "")
        fn_name = arn.split(":")[-1] if ":" in arn else arn
        mock    = scenario.get("lambda_mocks", {}).get(fn_name) or {}
        result  = mock.get("result", "Success")
        mock_attrs = {k: v for k, v in (mock.get("attributes") or {}).items() if v != ""}
        state.external.update(mock_attrs)

        if result == "Success":
            return default_next, "Success", f"Lambda '{fn_name}' → Success", False, ""
        else:
            return error_next or default_next, "Error", f"Lambda '{fn_name}' → Error", False, ""

    # ── DTMF / voice input ────────────────────────────────────────────────────
    if btype == "GetUserInput":
        valid_options = sorted({
            str(op)
            for c in conditions
            for op in ((c.get("Condition") or {}).get("Operands") or [])
            if (c.get("Condition") or {}).get("Operator") == "Equals"
        })

        # Look up in scenario by "Flow / Block" key
        block_key    = f"{flow_name} / {blabel}"
        dtmf_entry   = scenario.get("dtmf_inputs", {}).get(block_key)
        if dtmf_entry is None:
            # Fuzzy: match on block label alone
            for k, v in (scenario.get("dtmf_inputs") or {}).items():
                if blabel.rstrip("…")[:6] in k:
                    dtmf_entry = v
                    break

        if dtmf_entry is not None:
            input_val = (dtmf_entry.get("value", "") if isinstance(dtmf_entry, dict)
                         else str(dtmf_entry))
        elif interactive:
            input_val = prompt_choice(f"DTMF input [{blabel}]:", valid_options)
        else:
            input_val = valid_options[0] if valid_options else ""
            print(f"  [auto] DTMF '{blabel}': no scenario value — using '{input_val}'", file=sys.stderr)

        for cond in conditions:
            c        = cond.get("Condition") or {}
            operands = [str(o) for o in (c.get("Operands") or [])]
            if input_val in operands:
                return cond.get("NextAction", ""), f"Input: {input_val}", f"Input: {input_val}", False, ""

        return default_next, f"Input: {input_val} (no match)", f"Input: {input_val} → no match", False, ""

    # ── All other blocks — just continue ─────────────────────────────────────
    return default_next, "", "", False, ""


# ── Simulation loop ────────────────────────────────────────────────────────────

MAX_DEPTH  = 12
MAX_STEPS  = 200

def _run_flow(
    env: dict, state: SimState, scenario: dict,
    by_id: dict, by_name: dict, interactive: bool,
    path: list[Step], depth: int,
) -> None:
    if depth > MAX_DEPTH:
        path.append(Step(
            flow_id="", flow_name="", block_id="", block_label="[MAX DEPTH]",
            block_type="", type_label="", action_desc="Max sub-flow depth reached",
            branch="", terminal=True,
        ))
        return

    content    = env.get("content") or {}
    flow_name  = (env.get("metadata") or {}).get("name", "?")
    flow_id    = (env.get("metadata") or {}).get("id", "?")
    block_idx  = {a["Identifier"]: a for a in (content.get("Actions") or [])}
    current_id = content.get("StartAction", "")
    visited    : set[str] = set()

    while current_id and len(path) < MAX_STEPS:
        if current_id in visited:
            path.append(Step(
                flow_id=flow_id, flow_name=flow_name,
                block_id=current_id, block_label="[LOOP]",
                block_type="", type_label="",
                action_desc="Loop detected — stopping", branch="",
                terminal=True,
            ))
            break
        visited.add(current_id)

        action = block_idx.get(current_id)
        if not action:
            path.append(Step(
                flow_id=flow_id, flow_name=flow_name,
                block_id=current_id, block_label=f"[MISSING {current_id[:10]}]",
                block_type="", type_label="",
                action_desc="Block not found in flow", branch="",
                terminal=True,
            ))
            break

        btype  = action.get("Type", "")
        blabel = _block_label(action)

        next_id, branch, action_desc, is_terminal, transfer_target = execute_block(
            action, flow_name, state, scenario, interactive
        )

        path.append(Step(
            flow_id=flow_id, flow_name=flow_name,
            block_id=current_id, block_label=blabel,
            block_type=btype, type_label=TYPE_LABELS.get(btype, btype),
            action_desc=action_desc, branch=branch,
            next_block_id=next_id,
            terminal=is_terminal,
            is_transfer=bool(transfer_target),
            transfer_target=transfer_target,
        ))

        if is_terminal:
            break

        if transfer_target:
            sub = by_id.get(transfer_target) or find_flow(transfer_target, by_id, by_name)
            if sub:
                _run_flow(sub, state, scenario, by_id, by_name, interactive, path, depth + 1)
            else:
                path.append(Step(
                    flow_id=flow_id, flow_name=flow_name,
                    block_id=current_id, block_label=blabel,
                    block_type="", type_label="",
                    action_desc=f"Target flow not in cache: {transfer_target[:20]}",
                    branch="", terminal=True,
                ))
            break

        current_id = next_id


def simulate(
    start_flow_name: str,
    scenario: dict,
    by_id: dict,
    by_name: dict,
    interactive: bool,
) -> tuple[list[Step], SimState]:
    state = SimState(
        attributes=dict(scenario.get("attributes") or {}),
        contact_params=dict(scenario.get("call_parameters") or {}),
    )
    # Pre-strip empty strings so they don't shadow unset attrs
    state.attributes = {k: v for k, v in state.attributes.items() if v != ""}

    start_env = find_flow(start_flow_name, by_id, by_name)
    if not start_env:
        print(f"Error: flow '{start_flow_name}' not found in cache.", file=sys.stderr)
        sys.exit(1)

    path: list[Step] = []
    _run_flow(start_env, state, scenario, by_id, by_name, interactive, path, depth=0)
    return path, state


# ── Text output ────────────────────────────────────────────────────────────────

_KIND_COLOR = {
    "Lambda":           "\033[33m",   # yellow
    "Check Attribute":  "\033[34m",   # blue
    "Check Hours":      "\033[35m",   # magenta
    "Check Staffing":   "\033[35m",
    "Get Input":        "\033[36m",   # cyan
    "Disconnect":       "\033[31m",   # red
    "Transfer to Queue":"\033[32m",   # green
    "Transfer to Flow": "\033[36m",
    "Set Attribute":    "\033[32m",
    "Update Attribute": "\033[32m",
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"


def print_trace(path: list[Step], state: SimState, scenario: dict) -> None:
    cp = scenario.get("call_parameters") or {}
    print(f"\n{_BOLD}Simulation Trace{_RESET}")
    print(f"  ANI:     {cp.get('ani') or '(unknown)'}")
    print(f"  DNIS:    {cp.get('dnis') or '(unknown)'}")
    print(f"  Channel: {cp.get('channel', 'VOICE')}")
    print()

    cur_flow = None
    for i, step in enumerate(path, 1):
        if step.flow_name != cur_flow:
            cur_flow = step.flow_name
            print(f"  {_DIM}── {cur_flow} ──{_RESET}")

        color = _KIND_COLOR.get(step.type_label, "")
        tl    = f"{color}{step.type_label or step.block_type:<18}{_RESET}"
        label = f"{_BOLD}{step.block_label}{_RESET}" if step.block_label else ""
        desc  = step.action_desc or step.branch or ""
        term  = f"  {_DIM}[terminal]{_RESET}" if step.terminal else ""

        print(f"  {i:>3}.  {tl}  {label}")
        if desc:
            print(f"              {_DIM}{desc}{_RESET}{term}")
        elif term:
            print(f"              {term}")

    print()
    if state.queue:
        print(f"  {_BOLD}Final queue:{_RESET} {state.queue}")
    if state.attributes:
        print(f"  {_BOLD}Attributes set:{_RESET}")
        for k, v in sorted(state.attributes.items()):
            print(f"    {k} = '{v}'")
    print()


# ── HTML generation ────────────────────────────────────────────────────────────

def _build_graph(content: dict) -> tuple[dict, list, str]:
    """Minimal graph builder (mirrors flow_to_chart.py's build_graph)."""
    actions  = {a["Identifier"]: a for a in (content.get("Actions") or [])}
    start_id = content.get("StartAction", "")
    nodes    = {}
    for aid, action in actions.items():
        atype = action.get("Type", "")
        nodes[aid] = {
            "label":       TYPE_LABELS.get(atype, atype),
            "hint":        _param_hint(action),
            "is_decision": atype in DECISION_TYPES,
            "is_terminal": atype in TERMINAL_TYPES,
            "type":        atype,
        }
    edges = []
    for aid, action in actions.items():
        trans = action.get("Transitions") or {}
        nxt   = trans.get("NextAction")
        if nxt and nxt in actions:
            edges.append({"src": aid, "dst": nxt, "label": ""})
        for err in (trans.get("Errors") or []):
            dst = err.get("NextAction")
            if dst and dst in actions:
                etype = err.get("ErrorType", "Error").replace("NoMatchingError", "No Match")
                edges.append({"src": aid, "dst": dst, "label": etype})
        for cond in (trans.get("Conditions") or []):
            dst = cond.get("NextAction")
            if dst and dst in actions:
                edges.append({"src": aid, "dst": dst, "label": _cond_label(cond.get("Condition") or {})})
    return nodes, edges, start_id


def _param_hint(action: dict) -> str:
    t = action.get("Type", "")
    p = action.get("Parameters") or {}
    def tail(s): return str(s or "").rstrip("/").split("/")[-1][:30]
    hints = {
        "MessageParticipant":    lambda: p.get("Text", "")[:30],
        "CheckAttribute":        lambda: p.get("Attribute") or p.get("AttributeToCheck") or "",
        "CheckContactAttributes":lambda: p.get("Attribute") or p.get("AttributeToCheck") or "",
        "Compare":               lambda: p.get("ComparisonValue") or "",
        "GetUserInput":          lambda: p.get("Text", "")[:30],
        "SetQueue":              lambda: tail(p.get("QueueId") or (p.get("Queue") or {}).get("Id") or ""),
        "InvokeLambdaFunction":  lambda: tail(p.get("LambdaFunctionARN") or ""),
        "InvokeExternalResource":lambda: tail(p.get("FunctionArn") or p.get("ResourceId") or ""),
        "UpdateContactAttributes":lambda: ", ".join(f"{k}" for k in list((p.get("Attributes") or {}).keys())[:2]),
        "SetAttributes":         lambda: ", ".join(f"{k}" for k in list((p.get("Attributes") or {}).keys())[:2]),
    }
    fn = hints.get(t)
    try:
        return fn()[:40] if fn else ""
    except Exception:
        return ""


def build_html(path: list[Step], state: SimState, scenario: dict, by_id: dict, by_name: dict) -> str:
    # Collect unique flows in path order
    seen_flows: list[tuple[str, str]] = []  # (flow_id, flow_name)
    for step in path:
        if step.flow_id and (step.flow_id, step.flow_name) not in seen_flows:
            seen_flows.append((step.flow_id, step.flow_name))

    # Build per-flow visited sets
    flow_graphs: list[dict] = []
    for flow_id, flow_name in seen_flows:
        env = by_id.get(flow_id)
        if not env:
            continue
        content = env.get("content") or {}
        nodes, edges, start_id = _build_graph(content)

        flow_steps  = [s for s in path if s.flow_id == flow_id]
        visited_nids = {node_id(s.block_id) for s in flow_steps}
        visited_enids = {
            f"{node_id(s.block_id)}->{node_id(s.next_block_id)}"
            for s in flow_steps if s.next_block_id
        }

        elements: list[dict] = []
        if start_id:
            elements.append({"data": {"id": "START", "label": "Start", "ntype": "start", "visited": "y"}})
            elements.append({"data": {
                "id": "e_start", "source": "START",
                "target": node_id(start_id), "label": "",
                "visited": "y" if node_id(start_id) in visited_nids else "n",
            }})

        for uid, node in nodes.items():
            nid   = node_id(uid)
            label = node["label"] + ("\n" + node["hint"] if node["hint"] else "")
            ntype = ("terminal" if node["is_terminal"] else
                     "decision" if node["is_decision"] else "default")
            elements.append({"data": {
                "id": nid, "label": label, "ntype": ntype,
                "visited": "y" if nid in visited_nids else "n",
            }})

        for i, edge in enumerate(edges):
            src  = node_id(edge["src"])
            dst  = node_id(edge["dst"])
            ekey = f"{src}->{dst}"
            elements.append({"data": {
                "id": f"e{i}", "source": src, "target": dst,
                "label": edge["label"],
                "visited": "y" if ekey in visited_enids else "n",
            }})

        flow_graphs.append({
            "flow_id":   flow_id,
            "flow_name": flow_name,
            "elements":  elements,
        })

    # ── Step trace HTML ───────────────────────────────────────────────────────
    step_rows = []
    cur_flow = None
    for i, step in enumerate(path):
        if step.flow_name != cur_flow:
            cur_flow = step.flow_name
            step_rows.append(f'<div class="flow-divider">{_he(cur_flow)}</div>')

        color_cls = {
            "Lambda": "lam", "Check Attribute": "chk",
            "Check Hours": "hrs", "Check Staffing": "hrs",
            "Get Input": "inp", "Disconnect": "term",
            "Transfer to Queue": "queue", "Transfer to Flow": "xfer",
            "Update Attribute": "set", "Set Attribute": "set",
            "Set Queue": "set",
        }.get(step.type_label, "")

        term_badge = '<span class="badge-term">end</span>' if step.terminal else ""
        desc_html  = f'<div class="step-desc">{_he(step.action_desc)}</div>' if step.action_desc else ""

        step_rows.append(f"""<div class="step {color_cls}" data-bid="{_he(node_id(step.block_id))}" data-fid="{_he(step.flow_id)}">
  <span class="step-num">{i+1}</span>
  <div class="step-body">
    <div class="step-header"><span class="step-type">{_he(step.type_label or step.block_type)}</span> <span class="step-label">{_he(step.block_label)}</span>{term_badge}</div>
    {desc_html}
  </div>
</div>""")

    # ── Scenario summary ──────────────────────────────────────────────────────
    cp = scenario.get("call_parameters") or {}
    ani  = _he(cp.get("ani") or "(unknown)")
    dnis = _he(cp.get("dnis") or "(unknown)")
    chan = _he(cp.get("channel") or "VOICE")
    tm   = _he(cp.get("simulated_time") or "")
    final_queue = _he(state.queue or "none")

    # ── Tab buttons ───────────────────────────────────────────────────────────
    tab_buttons = "".join(
        f'<button class="tab-btn{" active" if i == 0 else ""}" onclick="showTab({i})">{_he(fg["flow_name"])}</button>'
        for i, fg in enumerate(flow_graphs)
    )
    cy_containers = "".join(
        f'<div id="cy{i}" class="cy-container{" active" if i == 0 else ""}"></div>'
        for i in range(len(flow_graphs))
    )
    graphs_json = json.dumps(flow_graphs, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Flow Simulation</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: system-ui, sans-serif; background: #0d1117; color: #c9d1d9; height: 100vh; display: flex; flex-direction: column; }}
.top-bar {{ background: #161b22; border-bottom: 1px solid #30363d; padding: 10px 18px; display: flex; align-items: center; gap: 16px; flex-shrink: 0; }}
.top-bar h1 {{ font-size: 1em; color: #58a6ff; }}
.meta-chip {{ background: #21262d; border-radius: 4px; padding: 3px 8px; font-size: 0.78em; color: #8b949e; }}
.meta-chip b {{ color: #c9d1d9; }}
.layout {{ display: flex; flex: 1; overflow: hidden; }}
.trace-panel {{ width: 360px; flex-shrink: 0; overflow-y: auto; background: #0d1117; border-right: 1px solid #30363d; padding: 10px 0; }}
.graph-panel {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}
.tab-bar {{ display: flex; gap: 2px; padding: 6px 10px; background: #161b22; border-bottom: 1px solid #30363d; flex-shrink: 0; }}
.tab-btn {{ padding: 4px 12px; border: 1px solid #30363d; border-radius: 4px; background: #21262d; color: #8b949e; cursor: pointer; font-size: 0.85em; }}
.tab-btn.active {{ background: #1f6feb; border-color: #1f6feb; color: white; }}
.cy-container {{ flex: 1; display: none; }}
.cy-container.active {{ display: block; }}
.flow-divider {{ font-size: 0.72em; color: #58a6ff; text-transform: uppercase; letter-spacing: 0.08em; padding: 8px 14px 4px; border-top: 1px solid #21262d; margin-top: 4px; }}
.flow-divider:first-child {{ border-top: none; margin-top: 0; }}
.step {{ display: flex; padding: 7px 14px; gap: 10px; border-left: 3px solid transparent; cursor: pointer; }}
.step:hover {{ background: #161b22; }}
.step.active-step {{ background: #1c2128 !important; }}
.step-num {{ color: #484f58; font-size: 0.78em; min-width: 22px; padding-top: 2px; text-align: right; }}
.step-body {{ flex: 1; min-width: 0; }}
.step-header {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
.step-type {{ font-size: 0.75em; color: #8b949e; }}
.step-label {{ font-size: 0.88em; color: #e6edf3; font-weight: 500; }}
.step-desc {{ font-size: 0.78em; color: #6e7681; margin-top: 2px; font-family: monospace; }}
.badge-term {{ background: #3d1f1f; color: #f85149; font-size: 0.7em; padding: 1px 5px; border-radius: 3px; font-weight: 600; }}
.step.lam  {{ border-left-color: #d29922; }}
.step.chk  {{ border-left-color: #388bfd; }}
.step.hrs  {{ border-left-color: #bc8cff; }}
.step.inp  {{ border-left-color: #39d353; }}
.step.term {{ border-left-color: #f85149; }}
.step.queue{{ border-left-color: #3fb950; }}
.step.xfer {{ border-left-color: #58a6ff; }}
.step.set  {{ border-left-color: #56d364; }}
</style>
</head>
<body>
<div class="top-bar">
  <h1>Flow Simulation</h1>
  <span class="meta-chip"><b>ANI</b> {ani}</span>
  <span class="meta-chip"><b>DNIS</b> {dnis}</span>
  <span class="meta-chip"><b>Channel</b> {chan}</span>
  <span class="meta-chip"><b>Time</b> {tm}</span>
  <span class="meta-chip"><b>Final Queue</b> {final_queue}</span>
</div>
<div class="layout">
  <div class="trace-panel" id="trace">
    {"".join(step_rows)}
  </div>
  <div class="graph-panel">
    <div class="tab-bar">{tab_buttons}</div>
    {cy_containers}
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/cytoscape@3/dist/cytoscape.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dagre@0.8.5/dist/dagre.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
<script>
const flowGraphs = {graphs_json};
const cyInstances = {{}};

const STYLES = [
  {{ selector: 'node', style: {{
    label: 'data(label)', 'text-valign': 'center', 'text-halign': 'center',
    'text-wrap': 'wrap', 'text-max-width': '140px', 'font-size': '10px',
    'width': 'label', 'height': 'label', 'padding': '10px',
    'shape': 'roundrectangle', 'border-width': 1,
    'background-color': '#21262d', 'border-color': '#30363d', 'color': '#484f58', opacity: 0.35,
  }}}},
  {{ selector: 'node[visited="y"]', style: {{
    'background-color': '#1a3a1a', 'border-color': '#3fb950', 'color': '#c9d1d9', opacity: 1,
  }}}},
  {{ selector: 'node[visited="y"][ntype="decision"]', style: {{
    shape: 'diamond', 'padding': '20px', 'text-max-width': '100px',
    'background-color': '#2a1f00', 'border-color': '#d29922', 'color': '#c9d1d9',
  }}}},
  {{ selector: 'node[visited="y"][ntype="terminal"]', style: {{
    shape: 'ellipse', 'background-color': '#3d1f1f', 'border-color': '#f85149', 'color': '#c9d1d9',
  }}}},
  {{ selector: 'node[ntype="start"]', style: {{
    shape: 'ellipse', 'font-weight': 'bold',
    'background-color': '#1f3a1f', 'border-color': '#3fb950', 'color': '#c9d1d9', opacity: 1,
  }}}},
  {{ selector: 'node[ntype="decision"]', style: {{ shape: 'diamond', 'padding': '20px', 'text-max-width': '100px' }}}},
  {{ selector: 'node[ntype="terminal"]', style: {{ shape: 'ellipse' }}}},
  {{ selector: 'node.highlighted', style: {{ 'border-width': 3, 'border-color': '#58a6ff', opacity: 1 }}}},
  {{ selector: 'edge', style: {{
    label: 'data(label)', 'font-size': '8px', color: '#484f58',
    'text-background-color': '#0d1117', 'text-background-opacity': 0.85, 'text-background-padding': '2px',
    'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
    'arrow-scale': 0.7, 'line-color': '#21262d', 'target-arrow-color': '#21262d', width: 1, opacity: 0.3,
  }}}},
  {{ selector: 'edge[visited="y"]', style: {{
    'line-color': '#3fb950', 'target-arrow-color': '#3fb950', width: 2.5, opacity: 1, color: '#8b949e',
  }}}},
];

function initCy(idx) {{
  if (cyInstances[idx]) return;
  const fg  = flowGraphs[idx];
  const ctr = document.getElementById('cy' + idx);
  const cy  = cytoscape({{
    container: ctr, elements: fg.elements, wheelSensitivity: 0.3,
    style: STYLES,
    layout: {{ name: 'dagre', rankDir: 'TB', nodeSep: 50, rankSep: 70, padding: 30, animate: false, fit: true }},
  }});
  cyInstances[idx] = {{ cy, flowId: fg.flow_id }};
}}

function showTab(idx) {{
  document.querySelectorAll('.cy-container').forEach((el, i) => {{
    el.classList.toggle('active', i === idx);
  }});
  document.querySelectorAll('.tab-btn').forEach((el, i) => {{
    el.classList.toggle('active', i === idx);
  }});
  initCy(idx);
  setTimeout(() => {{ if (cyInstances[idx]) cyInstances[idx].cy.fit(undefined, 30); }}, 50);
}}

// Step click → highlight node in graph
document.querySelectorAll('.step[data-bid]').forEach(el => {{
  el.addEventListener('click', () => {{
    const bid = el.dataset.bid;
    const fid = el.dataset.fid;
    // Switch to correct tab
    const tabIdx = flowGraphs.findIndex(fg => fg.flow_id === fid);
    if (tabIdx >= 0) {{
      showTab(tabIdx);
      setTimeout(() => {{
        const inst = cyInstances[tabIdx];
        if (inst) {{
          inst.cy.nodes().removeClass('highlighted');
          const n = inst.cy.getElementById(bid);
          if (n.length) {{ n.addClass('highlighted'); inst.cy.center(n); }}
        }}
      }}, 80);
    }}
    document.querySelectorAll('.step').forEach(s => s.classList.remove('active-step'));
    el.classList.add('active-step');
  }});
}});

// Init first tab
if (flowGraphs.length > 0) initCy(0);
</script>
</body>
</html>"""


# ── JSON output ────────────────────────────────────────────────────────────────

def to_json(path: list[Step], state: SimState, scenario: dict) -> dict:
    cp = scenario.get("call_parameters") or {}
    return {
        "call_parameters": cp,
        "step_count":      len(path),
        "final_queue":     state.queue,
        "final_attributes": state.attributes,
        "path": [
            {
                "step":          i + 1,
                "flow":          s.flow_name,
                "block_id":      s.block_id,
                "block_label":   s.block_label,
                "block_type":    s.block_type,
                "action":        s.action_desc,
                "branch":        s.branch,
                "terminal":      s.terminal,
            }
            for i, s in enumerate(path)
        ],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    p = argparse.ArgumentParser(
        description="Simulate a contact's path through Amazon Connect flows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --flow "Main IVR" --scenario scenario.json
  %(prog)s --instance-id <UUID> --flow "Main IVR" --scenario scenario.json --interactive
  %(prog)s --instance-id <UUID> --flow "Main IVR" --scenario scenario.json --json
        """,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--flow",        required=True, metavar="NAME", help="Starting flow name")
    p.add_argument("--scenario",    required=True, metavar="FILE", help="Scenario JSON file")
    p.add_argument("--interactive", action="store_true", help="Prompt at unresolved decision points")
    p.add_argument("--save-choices",action="store_true", help="Write resolved choices back to scenario file")
    p.add_argument("--html",        default=None,  metavar="FILE", help="HTML output path")
    p.add_argument("--no-html",     action="store_true")
    p.add_argument("--output",      default=None,  metavar="FILE", help="Save text trace to file")
    p.add_argument("--json",        action="store_true", dest="output_json")
    args = p.parse_args()

    # Load scenario
    try:
        scenario = json.loads(Path(args.scenario).read_text())
    except FileNotFoundError:
        print(f"Error: scenario file not found: {args.scenario}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in scenario: {e}", file=sys.stderr)
        sys.exit(1)

    # Load cache
    by_id, by_name = load_flow_cache(args.instance_id)
    if not by_id:
        print(
            f"Error: no cached flows found for instance {args.instance_id}.\n"
            f"Run flow_map.py first: python flow_map.py --instance-id {args.instance_id} --region <region>",
            file=sys.stderr,
        )
        sys.exit(1)

    # Simulate
    path, state = simulate(args.flow, scenario, by_id, by_name, args.interactive)

    # Output
    if args.output_json:
        print(json.dumps(to_json(path, state, scenario), indent=2))
    else:
        if args.output:
            import io as _io
            buf = _io.StringIO()
            _orig = sys.stdout
            sys.stdout = buf
            print_trace(path, state, scenario)
            sys.stdout = _orig
            Path(args.output).write_text(re.sub(r"\x1b\[[0-9;]*[mK]", "", buf.getvalue()))
            print(f"  Trace saved → {args.output}")
        else:
            print_trace(path, state, scenario)

    if not args.no_html:
        SIMULATIONS_DIR.mkdir(parents=True, exist_ok=True)
        html_path = args.html or str(SIMULATIONS_DIR / ("sim_" + re.sub(r"[^a-zA-Z0-9_-]", "_", args.flow) + ".html"))
        Path(html_path).write_text(build_html(path, state, scenario, by_id, by_name), encoding="utf-8")
        print(f"  HTML saved     → {html_path}")


if __name__ == "__main__":
    main()
