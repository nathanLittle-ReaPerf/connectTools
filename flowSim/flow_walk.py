#!/usr/bin/env python3
"""flow_walk.py — Interactive step-by-step contact flow walker.

Walks a contact flow block by block with live prompts at every decision
point: DTMF inputs, Lambda results (with parameter display), hours-of-
operation and staffing checks.  At the end you can save all captured
values as a scenario file ready for flow_sim.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FLOWSIM_DIR   = Path(__file__).parent
SCENARIOS_DIR = FLOWSIM_DIR / "Scenarios"

# ── Import shared engine from flow_sim ────────────────────────────────────────
sys.path.insert(0, str(FLOWSIM_DIR))
import flow_sim as _fs  # noqa: E402

load_flow_cache  = _fs.load_flow_cache
find_flow        = _fs.find_flow
SimState         = _fs.SimState
Step             = _fs.Step
resolve          = _fs.resolve
evaluate         = _fs.evaluate
TYPE_LABELS      = _fs.TYPE_LABELS
DECISION_TYPES   = _fs.DECISION_TYPES
TERMINAL_TYPES   = _fs.TERMINAL_TYPES
LAMBDA_TYPES     = _fs.LAMBDA_TYPES
SET_ATTR_TYPES   = _fs.SET_ATTR_TYPES
TRANSFER_TYPES   = _fs.TRANSFER_TYPES
_block_label     = _fs._block_label
_cond_label      = _fs._cond_label
SIMULATIONS_DIR  = _fs.SIMULATIONS_DIR

MAX_DEPTH = 12
MAX_STEPS = 300

# ── ANSI ──────────────────────────────────────────────────────────────────────
_R  = "\033[0m"
_B  = "\033[1m"
_D  = "\033[2m"
_CY = "\033[36m"
_YL = "\033[33m"
_GR = "\033[32m"
_RD = "\033[31m"
_BL = "\033[34m"
_MG = "\033[35m"

_KIND_COLOR: dict[str, str] = {
    "Lambda":            _YL,
    "Check Attribute":   _BL,
    "Check Hours":       _MG,
    "Check Staffing":    _MG,
    "Get Input":         _CY,
    "Disconnect":        _RD,
    "Transfer to Queue": _GR,
    "Transfer to Flow":  _CY,
    "Invoke Flow":       _CY,
    "Set Attribute":     _GR,
    "Update Attribute":  _GR,
    "Set Queue":         _GR,
}


# ── Walk session (accumulates choices for scenario export) ────────────────────
@dataclass
class WalkSession:
    dtmf_inputs:            dict       = field(default_factory=dict)
    lambda_mocks:           dict       = field(default_factory=dict)
    hours_mocks:            dict       = field(default_factory=dict)
    staffing_mocks:         dict       = field(default_factory=dict)
    # fn_name → list of $.External attr names expected from that Lambda
    lambda_output_templates: dict      = field(default_factory=dict)
    path:                   list       = field(default_factory=list)  # list[Step]
    step_count:             int        = 0


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _ask(label: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"       {label}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return val if val else default


def _ask_choice(label: str, options: list[str], default: str = "") -> str:
    opts_str = "/".join(options) if options else "free input"
    hint     = f"[{opts_str}]" + (f" default: {default}" if default else "")
    while True:
        try:
            val = input(f"       {label} {hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default or (options[0] if options else "")
        if not val and default:
            return default
        if not options or val in options:
            return val
        print(f"         Invalid — choose: {opts_str}")


def _ask_bool(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        val = input(f"       {label} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not val:
        return default
    return val in ("y", "yes", "1", "true")


# ── Display helpers ───────────────────────────────────────────────────────────

def _divider(flow_name: str) -> None:
    try:
        import os as _os
        w = min(64, _os.get_terminal_size(2).columns - 4)
    except Exception:
        w = 60
    pad = max(0, w - len(flow_name) - 4)
    print(f"\n  {_D}── {flow_name} {'─' * pad}{_R}")


def _step_header(num: int, type_label: str, block_type: str, label: str) -> None:
    color = _KIND_COLOR.get(type_label, "")
    tl    = f"{color}{(type_label or block_type):<18}{_R}"
    print(f"  {_D}{num:>3}.{_R}  {tl}  {_B}{label}{_R}")


def _detail(text: str, color: str = "") -> None:
    c = color or _D
    print(f"         {c}{text}{_R}")


def _result(text: str, ok: bool = True) -> None:
    col = _GR if ok else _RD
    print(f"         {col}→ {text}{_R}")


# ── Lambda input parameter resolution ────────────────────────────────────────

def _lambda_input_params(params: dict, state: SimState) -> dict[str, str]:
    """Resolve custom parameters passed to the Lambda function."""
    raw    = params.get("Parameter") or {}
    result: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            result[k] = resolve(str(v), state)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                result[item.get("Name", "?")] = resolve(item.get("Value", ""), state)
    return result


# ── Lambda output attribute detection ────────────────────────────────────────

_EXT_RE = re.compile(r'\$\.External\.([a-zA-Z0-9_]+)')


def _detect_lambda_outputs(content: dict) -> list[str]:
    """Scan all block Parameters in this flow for $.External.* references.

    Returns a deduplicated, sorted list of attribute names — these are the
    external attributes that downstream blocks are expected to read after a
    Lambda invocation.
    """
    found: set[str] = set()
    for action in (content.get("Actions") or []):
        params = action.get("Parameters") or {}
        # Stringify the whole params dict and scan for $.External.* patterns
        for m in _EXT_RE.finditer(json.dumps(params)):
            found.add(m.group(1))
    return sorted(found)


# ── Block walker ──────────────────────────────────────────────────────────────

def _walk_block(
    action: dict,
    flow_name: str,
    flow_content: dict,
    state: SimState,
    session: WalkSession,
    num: int,
) -> tuple[str, bool, str, str, str]:
    """
    Interactively process one block.
    Returns (next_block_id, is_terminal, transfer_target, action_desc, branch).
    transfer_target is non-empty for TransferContactToFlow blocks.
    flow_content is the full content dict for the current flow (used to
    detect expected Lambda output attributes).
    """
    btype  = action.get("Type", "")
    blabel = _block_label(action)
    tl     = TYPE_LABELS.get(btype, btype)
    params = action.get("Parameters") or {}
    trans  = action.get("Transitions") or {}

    default_next = trans.get("NextAction", "")
    errors       = trans.get("Errors") or []
    conditions   = trans.get("Conditions") or []
    error_next   = errors[0].get("NextAction", "") if errors else ""

    _step_header(num, tl, btype, blabel)

    # ── Terminal ──────────────────────────────────────────────────────────────
    if btype in TERMINAL_TYPES:
        if btype == "TransferContactToQueue":
            q = (params.get("QueueId") or (params.get("Queue") or {}).get("Id") or
                 state.queue or "configured queue")
            _result(f"Transferred to queue: {q}")
            return "", True, "", f"→ Queue: {q}", ""
        elif btype == "DisconnectParticipant":
            _result("Contact disconnected", ok=False)
            return "", True, "", "Contact disconnected", ""
        else:
            _result("Flow ended", ok=False)
            return "", True, "", "Flow ended", ""

    # ── Transfer to sub-flow ──────────────────────────────────────────────────
    if btype in TRANSFER_TYPES:
        target = params.get("ContactFlowId") or params.get("FlowModuleId") or ""
        _result(f"→ sub-flow: {target[:40] or '?'}")
        return "", False, target, f"Transfer to flow: {target[:30] or '?'}", "→ sub-flow"

    # ── Set attributes ────────────────────────────────────────────────────────
    if btype in SET_ATTR_TYPES:
        parts = []
        for key, val in (params.get("Attributes") or {}).items():
            resolved = resolve(val, state)
            chosen = _ask(f"  {key}", default=resolved)
            state.attributes[key] = chosen
            _detail(f"SET {key} = '{chosen}'", _GR)
            parts.append(f"{key}='{chosen}'")
        return default_next, False, "", "SET " + (", ".join(parts) or "(none)"), ""

    # ── Set queue ─────────────────────────────────────────────────────────────
    if btype == "SetQueue":
        q = params.get("QueueId") or (params.get("Queue") or {}).get("Id") or ""
        state.queue = q
        _detail(f"Queue → {q or '?'}", _GR)
        return default_next, False, "", f"Queue → {q or '?'}", ""

    # ── Play message ──────────────────────────────────────────────────────────
    if btype == "MessageParticipant":
        text = resolve(
            params.get("Text") or (params.get("Prompt") or {}).get("Text") or "", state
        )
        if text:
            _detail(f'"{text[:120]}"')
        return default_next, False, "", f'"{text[:60]}"' if text else "", ""

    # ── Check attribute ───────────────────────────────────────────────────────
    if btype in ("CheckAttribute", "CheckContactAttributes", "Compare"):
        cmp_expr = (params.get("ComparisonValue") or params.get("Attribute") or
                    params.get("AttributeToCheck") or "")
        resolved = resolve(cmp_expr, state)
        _detail(f"checking: {cmp_expr}  (current: '{resolved}')")
        for cond in conditions:
            c   = cond.get("Condition") or {}
            ops = [str(o) for o in (c.get("Operands") or [])]
            if evaluate(resolved, c.get("Operator", "Equals"), ops):
                lbl = _cond_label(c)
                _result(f"match → {lbl}")
                return (cond.get("NextAction", ""), False, "",
                        f"'{cmp_expr}'='{resolved}' → {lbl}", lbl)
        # Show every condition that was tested so the user can see what's in the flow
        for cond in conditions:
            c   = cond.get("Condition") or {}
            op  = c.get("Operator", "?")
            ops = [str(o) for o in (c.get("Operands") or [])]
            _detail(f"  tested: {op} {ops}  → no match")
        _result("no match → default", ok=False)
        return default_next, False, "", f"'{cmp_expr}'='{resolved}' → no match", "no match"

    # ── Check hours of operation ──────────────────────────────────────────────
    if btype == "CheckHoursOfOperation":
        hoo_id = params.get("HoursOfOperationId") or ""
        _detail(f"Hours: {hoo_id[:52] or '?'}")
        in_hours = _ask_bool("In hours?", default=True)
        session.hours_mocks[hoo_id] = {"in_hours": in_hours}
        if in_hours:
            for cond in conditions:
                c   = cond.get("Condition") or {}
                ops = [str(o).lower() for o in (c.get("Operands") or [])]
                if any(o in ("true", "inhours") for o in ops):
                    _result("In Hours")
                    return cond.get("NextAction", default_next), False, "", "In Hours", "In Hours"
            _result("In Hours")
            return default_next, False, "", "In Hours", "In Hours"
        _result("Out of Hours", ok=False)
        return error_next or default_next, False, "", "Out of Hours", "Out of Hours"

    # ── Check staffing ────────────────────────────────────────────────────────
    if btype in ("CheckStaffing", "CheckStaffingStatus"):
        q_id = params.get("QueueId") or ""
        _detail(f"Queue: {q_id[:52] or '?'}")
        staffed = _ask_bool("Staffed?", default=True)
        session.staffing_mocks[q_id] = {"staffed": staffed}
        if staffed:
            for cond in conditions:
                c   = cond.get("Condition") or {}
                ops = [str(o).lower() for o in (c.get("Operands") or [])]
                if any(o in ("true", "staffed") for o in ops):
                    _result("Staffed")
                    return cond.get("NextAction", default_next), False, "", "Staffed", "Staffed"
            _result("Staffed")
            return default_next, False, "", "Staffed", "Staffed"
        _result("Not Staffed", ok=False)
        return error_next or default_next, False, "", "Not Staffed", "Not Staffed"

    # ── Lambda ────────────────────────────────────────────────────────────────
    if btype in LAMBDA_TYPES:
        arn     = (params.get("LambdaFunctionARN") or params.get("FunctionArn") or
                   params.get("ResourceId") or "")
        fn_name = arn.split(":")[-1] if ":" in arn else arn
        _detail(f"ARN:  {arn or '?'}")

        lp = _lambda_input_params(params, state)
        if lp:
            _detail("Input parameters:")
            for k, v in lp.items():
                _detail(f"  {k} = '{v}'")

        # ── On first encounter: establish expected output attribute names ──
        if fn_name not in session.lambda_output_templates:
            detected = _detect_lambda_outputs(flow_content)
            if detected:
                _detail(f"Detected $.External attrs referenced in flow: {', '.join(detected)}")
            suggested = ", ".join(detected)
            raw = _ask("Return attributes (comma-separated, blank for none)", default=suggested)
            template = [a.strip() for a in raw.split(",") if a.strip()] if raw.strip() else []
            session.lambda_output_templates[fn_name] = template

        result = _ask_choice("Result", ["Success", "Error"], default="Success")
        mock: dict = {"result": result, "attributes": {}}
        session.lambda_mocks[fn_name] = mock

        if result == "Success":
            template = session.lambda_output_templates.get(fn_name) or []
            if template:
                for attr_name in template:
                    attr_val = _ask(f"  $.External.{attr_name}")
                    mock["attributes"][attr_name] = attr_val
                    state.external[attr_name] = attr_val
                    _detail(f"  $.External.{attr_name} = '{attr_val}'", _GR)
                extra = _ask("  Additional attribute name (blank to skip)")
                while extra.strip():
                    attr_val = _ask(f"  $.External.{extra.strip()}")
                    mock["attributes"][extra.strip()] = attr_val
                    state.external[extra.strip()] = attr_val
                    _detail(f"  $.External.{extra.strip()} = '{attr_val}'", _GR)
                    extra = _ask("  Additional attribute name (blank to skip)")
            else:
                _detail("(blank name to finish)")
                while True:
                    attr_name = _ask("  Attribute name").strip()
                    if not attr_name:
                        break
                    attr_val = _ask(f"  {attr_name}")
                    mock["attributes"][attr_name] = attr_val
                    state.external[attr_name] = attr_val
                    _detail(f"  $.External.{attr_name} = '{attr_val}'", _GR)
            _result("Lambda → Success")
            return default_next, False, "", f"Lambda '{fn_name}' → Success", "Success"
        else:
            _result("Lambda → Error", ok=False)
            return error_next or default_next, False, "", f"Lambda '{fn_name}' → Error", "Error"

    # ── DTMF / voice input ────────────────────────────────────────────────────
    if btype in ("GetUserInput", "GetParticipantInput"):
        text = resolve(params.get("Text") or "", state)
        if text:
            _detail(f'"{text[:120]}"')
        valid = sorted({
            str(op)
            for c in conditions
            for op in ((c.get("Condition") or {}).get("Operands") or [])
            if (c.get("Condition") or {}).get("Operator") == "Equals"
        })
        val = _ask_choice("DTMF input", valid, default=valid[0] if valid else "")
        session.dtmf_inputs[f"{flow_name} / {blabel}"] = {"value": val}
        for cond in conditions:
            c   = cond.get("Condition") or {}
            ops = [str(o) for o in (c.get("Operands") or [])]
            if val in ops:
                _result(f"Input: {val}")
                return (cond.get("NextAction", ""), False, "",
                        f"Input: {val}", f"Input: {val}")
        _result(f"Input: {val} (no match → default)", ok=False)
        return default_next, False, "", f"Input: {val} → no match", f"Input: {val} (no match)"

    # ── All other blocks ──────────────────────────────────────────────────────
    return default_next, False, "", "", ""


# ── Walking loop ──────────────────────────────────────────────────────────────

def _walk_flow(
    env: dict,
    state: SimState,
    session: WalkSession,
    by_id: dict,
    by_name: dict,
    depth: int,
) -> None:
    if depth > MAX_DEPTH:
        print(f"\n  {_RD}[MAX DEPTH REACHED]{_R}")
        return

    content    = env.get("content") or {}
    meta       = env.get("metadata") or {}
    flow_name  = meta.get("name", "?")
    flow_id    = meta.get("id", "")
    block_idx  = {a["Identifier"]: a for a in (content.get("Actions") or [])}
    current_id = content.get("StartAction", "")
    visited: set[str] = set()

    _divider(flow_name)

    while current_id and session.step_count < MAX_STEPS:
        if current_id in visited:
            print(f"\n  {_RD}[LOOP DETECTED — stopping]{_R}")
            break
        visited.add(current_id)

        action = block_idx.get(current_id)
        if not action:
            print(f"\n  {_RD}[MISSING BLOCK: {current_id[:10]}]{_R}")
            break

        session.step_count += 1
        btype = action.get("Type", "")
        next_id, is_terminal, transfer_target, action_desc, branch = _walk_block(
            action, flow_name, content, state, session, session.step_count
        )

        session.path.append(Step(
            flow_id       = flow_id,
            flow_name     = flow_name,
            block_id      = current_id,
            block_label   = _block_label(action),
            block_type    = btype,
            type_label    = TYPE_LABELS.get(btype, btype),
            action_desc   = action_desc,
            branch        = branch,
            next_block_id = next_id,
            terminal      = is_terminal,
            is_transfer   = bool(transfer_target),
            transfer_target = transfer_target,
        ))

        if is_terminal:
            break

        if transfer_target:
            sub = by_id.get(transfer_target) or find_flow(transfer_target, by_id, by_name)
            if sub:
                _walk_flow(sub, state, session, by_id, by_name, depth + 1)
            else:
                print(f"\n  {_RD}[Target flow not in cache: {transfer_target[:40]}]{_R}")
            break

        current_id = next_id


# ── Save session as scenario ──────────────────────────────────────────────────

def _save_scenario(state: SimState, session: WalkSession,
                   instance_id: str, flow_name: str) -> None:
    default_name = re.sub(r"[^a-z0-9]+", "_", flow_name.lower()).strip("_")[:24]
    name = _ask("Scenario name", default=default_name) or default_name

    scenario = {
        "_name":          name,
        "_source":        "flow_walk",
        "_flow":          flow_name,
        "call_parameters": state.contact_params,
        "attributes":     dict(state.attributes),
        "dtmf_inputs":    session.dtmf_inputs,
        "lambda_mocks":   session.lambda_mocks,
        "hours_mocks":    session.hours_mocks,
        "staffing_mocks": session.staffing_mocks,
    }

    out_dir = SCENARIOS_DIR / instance_id
    out_dir.mkdir(parents=True, exist_ok=True)

    date_pfx = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path  = out_dir / f"walk_{date_pfx}_{name}.json"
    n = 2
    while out_path.exists():
        out_path = out_dir / f"walk_{date_pfx}_{name}_{n}.json"
        n += 1

    out_path.write_text(json.dumps(scenario, indent=2), encoding="utf-8")
    print(f"\n  {_GR}Saved → {out_path}{_R}")
    print(f"  {_D}Run with:{_R}")
    print(f"  {_D}  python flow_sim.py --instance-id {instance_id} "
          f"--flow \"{flow_name}\" --scenario \"{out_path}\"{_R}")


# ── Main entry point ──────────────────────────────────────────────────────────

def walk(
    instance_id: str,
    flow_name: str,
    initial_attrs: dict | None = None,
    contact_params: dict | None = None,
) -> None:
    """Interactive flow walk. Callable from the flowsim CLI or standalone."""
    by_id, by_name = load_flow_cache(instance_id)
    if not by_id:
        print(
            f"  Error: no cached flows for instance {instance_id}.\n"
            f"  Run: python flow_map.py --instance-id {instance_id} --region <region>",
            file=sys.stderr,
        )
        sys.exit(1)

    start = find_flow(flow_name, by_id, by_name)
    if not start:
        print(f"  Error: flow '{flow_name}' not found in cache.", file=sys.stderr)
        sys.exit(1)

    actual_name = (start.get("metadata") or {}).get("name", flow_name)

    state = SimState(
        attributes   =dict(initial_attrs or {}),
        contact_params=dict(contact_params or {}),
    )
    session = WalkSession()

    # Prompt for call parameters not already supplied
    if not state.contact_params.get("ani"):
        ani = _ask("Caller number (ANI)", default="+15551234567")
        if ani:
            state.contact_params["ani"] = ani
    if not state.contact_params.get("dnis"):
        dnis = _ask("Dialed number (DNIS)", default="")
        if dnis:
            state.contact_params["dnis"] = dnis

    print(f"\n  {_B}Walking: {actual_name}{_R}")
    print(f"  {_D}ANI:  {state.contact_params.get('ani') or '?'}  "
          f"DNIS: {state.contact_params.get('dnis') or '?'}{_R}")
    print(f"  {_D}Ctrl+C to stop early{_R}")

    try:
        _walk_flow(start, state, session, by_id, by_name, depth=0)
    except KeyboardInterrupt:
        print(f"\n\n  {_D}Walk interrupted.{_R}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n  {_B}Walk complete — {session.step_count} steps{_R}")
    if state.queue:
        print(f"  {_B}Final queue:{_R}  {state.queue}")
    if state.attributes:
        print(f"  {_B}Attributes captured:{_R}")
        for k, v in sorted(state.attributes.items()):
            print(f"    {k} = '{v}'")
    if state.external:
        print(f"  {_B}Lambda outputs:{_R}")
        for k, v in sorted(state.external.items()):
            print(f"    {k} = '{v}'")

    # ── HTML visualization ────────────────────────────────────────────────────
    if session.path:
        SIMULATIONS_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", actual_name)
        html_path = SIMULATIONS_DIR / f"walk_{safe_name}.html"
        scenario_for_html = {
            "call_parameters": state.contact_params,
            "attributes":      state.attributes,
        }
        html = _fs.build_html(session.path, state, scenario_for_html, by_id, by_name)
        html_path.write_text(html, encoding="utf-8")
        print(f"  {_GR}HTML saved → {html_path}{_R}")

    print()
    if _ask_bool("Save session as scenario?", default=True):
        _save_scenario(state, session, instance_id, actual_name)


# ── CLI ───────────────────────────────────────────────────────────────────────

_MAN = """\
NAME
    flow_walk.py — Interactive step-by-step Amazon Connect flow walker

SYNOPSIS
    python flow_walk.py --instance-id UUID --flow NAME [OPTIONS]

DESCRIPTION
    Walks a contact flow block by block with live prompts at every decision
    point.  At hours-of-operation and staffing checks you answer Y/n.  At
    DTMF menus you type the digit.  At Lambda invocations you see the
    resolved input parameters, choose Success or Error, and optionally set
    the output attributes the Lambda would have returned.

    Non-interactive blocks (Set Attribute, Play Message, Set Queue,
    Check Attribute) execute automatically; their outcomes are printed.

    At the end of the walk you can save all captured values as a
    scenario file ready for use with flow_sim.py.

OPTIONS
    --instance-id UUID   Connect instance UUID (flow cache must exist)
    --flow NAME          Starting flow (case-insensitive substring match)
    --ani NUMBER         Caller phone number  (prompted if omitted)
    --dnis NUMBER        Dialed phone number  (optional)
    --attr KEY=VALUE     Pre-set a contact attribute (repeatable)

EXAMPLES
    python flow_walk.py --instance-id <UUID> --flow "Main IVR"
    python flow_walk.py --instance-id <UUID> --flow "Main IVR" --ani +15551234567
    python flow_walk.py --instance-id <UUID> --flow "Billing" --attr customer_type=premium
"""


def main() -> None:
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    p = argparse.ArgumentParser(
        prog="flow_walk.py",
        description="Interactive step-by-step Amazon Connect flow walker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_MAN,
    )
    p.add_argument("--instance-id", required=True, metavar="UUID")
    p.add_argument("--flow",        required=True, metavar="NAME",
                   help="Starting flow name (case-insensitive substring match)")
    p.add_argument("--ani",  default=None, metavar="NUMBER")
    p.add_argument("--dnis", default=None, metavar="NUMBER")
    p.add_argument("--attr", action="append", default=[], metavar="KEY=VALUE",
                   help="Pre-set a contact attribute (repeatable)")
    args = p.parse_args()

    initial_attrs: dict[str, str] = {}
    for kv in args.attr:
        if "=" in kv:
            k, _, v = kv.partition("=")
            initial_attrs[k.strip()] = v.strip()
        else:
            print(f"  Warning: ignoring --attr {kv!r} (expected KEY=VALUE)", file=sys.stderr)

    contact_params: dict[str, str] = {}
    if args.ani:  contact_params["ani"]  = args.ani
    if args.dnis: contact_params["dnis"] = args.dnis

    walk(args.instance_id, args.flow, initial_attrs, contact_params)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Interrupted.")
        sys.exit(0)
