#!/usr/bin/env python3
"""flow_review.py — AI-powered deep analysis of an Amazon Connect contact flow.

Sends a structured flow summary to the Claude API and returns plain-English
optimization recommendations covering UX, reliability, structure, and
AWS Connect best practices.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

_MAN = """\
NAME
    flow_review.py — AI-powered deep analysis of an Amazon Connect contact flow

SYNOPSIS
    python flow_review.py FLOW_JSON [OPTIONS]

DESCRIPTION
    Builds a structured summary of a contact flow and sends it to the Claude
    API for deep analysis. Returns plain-English recommendations that go beyond
    rule-based checks — understanding caller intent, identifying redundant paths,
    and suggesting architectural improvements.

    Requires the ANTHROPIC_API_KEY environment variable (or pass --api-key).

OPTIONS
    FLOW_JSON
        Exported flow JSON file (from export_flow.py or raw flow content).

    --model MODEL
        Claude model to use (default: claude-opus-4-6).

    --api-key KEY
        Anthropic API key. Defaults to the ANTHROPIC_API_KEY environment variable.

    --max-tokens N
        Maximum tokens in the response (default: 2048).

    --json
        Print raw JSON response to stdout (includes full API response metadata).

    --raw
        Print only the raw model text with no formatting.

EXAMPLES
    python flow_review.py Main_IVR.json
    python flow_review.py Main_IVR.json --model claude-sonnet-4-6
    python flow_review.py Main_IVR.json --json | jq '.recommendations'

NOTES
    API calls cost tokens. For large flows (>40 blocks) use --model claude-sonnet-4-6
    to reduce cost. Opus is the default for deeper reasoning quality.
    Run flow_optimize.py first for fast rule-based checks at no API cost.
"""

_DEFAULT_MODEL    = "claude-opus-4-6"
_DEFAULT_MAX_TOKENS = 2048

_SYSTEM_PROMPT = """\
You are an expert Amazon Connect contact flow architect with deep knowledge of \
IVR design, AWS Connect APIs, and call centre best practices.

You review contact flow configurations and provide actionable, specific \
optimization recommendations. You understand caller psychology, error handling \
patterns, and AWS Connect-specific constraints and capabilities.

Be concise and specific. Prioritise recommendations by impact. \
Avoid generic advice — every recommendation should reference specific blocks \
or patterns in the flow provided."""

_USER_PROMPT_TEMPLATE = """\
Review the following Amazon Connect contact flow and provide optimization recommendations.

Flow name: {name}
Flow type: {flow_type}
Total blocks: {block_count}

--- FLOW STRUCTURE ---
{flow_summary}
--- END FLOW STRUCTURE ---

Provide recommendations in these four categories. For each recommendation, \
name the specific block(s) involved and explain the impact:

1. **Caller Experience (UX)** — menu design, prompts, retry logic, wait messaging
2. **Reliability & Error Handling** — missing error paths, staffing checks, \
hours-of-operation, Lambda failure handling
3. **Flow Structure** — complexity, sub-flow opportunities, redundant paths, \
blocks that can be simplified
4. **AWS Connect Best Practices** — API usage, attribute management, \
Contact Lens setup, whisper flows, logging

If a category has no issues, say so briefly. \
End with a one-sentence overall assessment."""


# ── Flow summariser ───────────────────────────────────────────────────────────

def _param_summary(atype: str, params: dict) -> str:
    """Return a short human-readable summary of the key parameter for a block."""
    if atype == "PlayPrompt":
        text = params.get("Text") or params.get("TextToSpeechType") or ""
        return f'text: "{text[:80]}"' if text else ""
    if atype == "GetUserInput":
        text = params.get("Text") or ""
        return f'prompt: "{text[:80]}"' if text else ""
    if atype == "InvokeLambdaFunction":
        arn = params.get("LambdaFunctionARN") or ""
        return f"fn: {arn.split(':')[-1]}" if arn else ""
    if atype == "SetQueue":
        qid = (params.get("Queue") or {}).get("Id") or params.get("QueueId") or ""
        return f"queue: {qid.split('/')[-1]}" if qid else ""
    if atype in ("CheckAttribute", "CheckContactAttributes"):
        attr = params.get("Attribute") or (params.get("Attributes") or [{}])[0]
        name = attr.get("Name") or attr.get("Key") or ""
        return f"attribute: {name}" if name else ""
    if atype == "SetContactAttributes":
        attrs = params.get("Attributes") or {}
        if isinstance(attrs, dict):
            pairs = [f"{k}={v}" for k, v in list(attrs.items())[:2]]
            return "set: " + ", ".join(pairs) if pairs else ""
    if atype == "CheckHoursOfOperation":
        hid = (params.get("HoursOfOperation") or {}).get("Id") or ""
        return f"hours: {hid.split('/')[-1]}" if hid else ""
    if atype in ("TransferContactToFlow", "InvokeFlowModule"):
        fid = (params.get("ContactFlow") or {}).get("Id") or params.get("ContactFlowId") or ""
        return f"flow: {fid.split('/')[-1]}" if fid else ""
    return ""


def _transitions_summary(trans: dict, actions: dict) -> list:
    """Return list of transition strings like '→ BlockName', '→ 1: BlockName'."""
    lines = []
    nxt   = trans.get("NextAction") or ""
    if nxt:
        label = actions.get(nxt, {}).get("Identifier", nxt)
        lines.append(f"  → {label}")
    for c in (trans.get("Conditions") or []):
        dst   = c.get("NextAction") or ""
        label = actions.get(dst, {}).get("Identifier", dst) if dst else "(none)"
        cond  = (c.get("Condition") or {})
        val   = cond.get("Operands", ["?"])[0] if cond.get("Operands") else "?"
        lines.append(f"  → [{val}] {label}")
    for e in (trans.get("Errors") or []):
        dst   = e.get("NextAction") or ""
        label = actions.get(dst, {}).get("Identifier", dst) if dst else "(none)"
        etype = e.get("ErrorType") or "error"
        lines.append(f"  → [{etype}] {label}")
    return lines


def build_flow_summary(content: dict) -> str:
    """Build a compact text representation of the flow for the prompt."""
    raw     = content.get("Actions") or []
    actions = {a["Identifier"]: a for a in raw if "Identifier" in a}
    start   = content.get("StartAction") or ""

    lines = [f"Start: {start}", ""]

    # Walk from start first (BFS) then append any unreachable blocks
    visited = []
    queue   = [start] if start in actions else []
    seen    = set()
    while queue:
        aid = queue.pop(0)
        if aid in seen or aid not in actions:
            continue
        seen.add(aid)
        visited.append(aid)
        action = actions[aid]
        trans  = action.get("Transitions") or {}
        for nxt in ([trans.get("NextAction")] +
                    [c.get("NextAction") for c in (trans.get("Conditions") or [])] +
                    [e.get("NextAction") for e in (trans.get("Errors") or [])]):
            if nxt and nxt not in seen:
                queue.append(nxt)

    # Append any unreachable blocks at the end
    for aid in actions:
        if aid not in seen:
            visited.append(aid)

    for aid in visited:
        action  = actions[aid]
        atype   = action.get("Type", "?")
        params  = action.get("Parameters") or {}
        trans   = action.get("Transitions") or {}
        p_str   = _param_summary(atype, params)
        t_lines = _transitions_summary(trans, actions)

        header = f'[{aid}] {atype}'
        if p_str:
            header += f'  ({p_str})'
        lines.append(header)
        lines.extend(t_lines)
        lines.append("")

    return "\n".join(lines)


# ── File loader ───────────────────────────────────────────────────────────────

def load_flow(path: str) -> tuple:
    """Returns (name, flow_type, content_dict)."""
    p = Path(path).expanduser()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: file not found: {p}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if "content" in data and "Actions" in (data.get("content") or {}):
        meta    = data.get("metadata") or {}
        name    = meta.get("name") or p.stem
        ftype   = meta.get("type") or ""
        content = data["content"]
    elif "Actions" in data:
        name, ftype, content = p.stem, "", data
    else:
        print("Error: file does not look like a contact flow (no 'Actions' array).",
              file=sys.stderr)
        sys.exit(1)

    return name, ftype, content


# ── API call ──────────────────────────────────────────────────────────────────

def call_claude(prompt: str, model: str, max_tokens: int, api_key: str | None) -> dict:
    try:
        import anthropic
    except ImportError:
        print("Error: anthropic SDK not installed. Run: pip install anthropic --user",
              file=sys.stderr)
        sys.exit(1)

    kwargs = {}
    if api_key:
        kwargs["api_key"] = api_key

    client = anthropic.Anthropic(**kwargs)

    print(f"  Sending to {model}...", end="", flush=True, file=sys.stderr)
    try:
        msg = client.messages.create(
            model      = model,
            max_tokens = max_tokens,
            system     = _SYSTEM_PROMPT,
            messages   = [{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"\nError calling Claude API: {e}", file=sys.stderr)
        sys.exit(1)

    print(" done.", file=sys.stderr)
    return msg


# ── Output ────────────────────────────────────────────────────────────────────

def _hr(width=72):
    print("  " + "─" * width)


def print_review(name: str, model: str, text: str):
    _hr()
    print(f"  FLOW REVIEW   {name}")
    print(f"  Model: {model}")
    _hr()
    print()
    # Indent and print the response
    for line in text.splitlines():
        print(f"  {line}")
    print()
    _hr()
    print()


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="AI-powered deep analysis of an Amazon Connect contact flow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s Main_IVR.json
  %(prog)s Main_IVR.json --model claude-sonnet-4-6
  %(prog)s Main_IVR.json --json
        """,
    )
    p.add_argument("flow_file", metavar="FLOW_JSON")
    p.add_argument("--model",      default=_DEFAULT_MODEL,
                   help=f"Claude model (default: {_DEFAULT_MODEL})")
    p.add_argument("--api-key",    default=None,
                   help="Anthropic API key (default: ANTHROPIC_API_KEY env var)")
    p.add_argument("--max-tokens", default=_DEFAULT_MAX_TOKENS, type=int,
                   metavar="N", help=f"Max response tokens (default: {_DEFAULT_MAX_TOKENS})")
    p.add_argument("--json",  action="store_true", dest="output_json",
                   help="Print JSON output")
    p.add_argument("--raw",   action="store_true",
                   help="Print only raw model text")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    args = parse_args()

    name, ftype, content = load_flow(args.flow_file)
    n_blocks     = len(content.get("Actions") or [])
    flow_summary = build_flow_summary(content)

    prompt = _USER_PROMPT_TEMPLATE.format(
        name         = name,
        flow_type    = ftype or "unknown",
        block_count  = n_blocks,
        flow_summary = flow_summary,
    )

    print(f"  Flow    : {name}  ({n_blocks} blocks)", file=sys.stderr)

    msg  = call_claude(prompt, args.model, args.max_tokens, args.api_key)
    text = msg.content[0].text if msg.content else ""

    if args.output_json:
        print(json.dumps({
            "flow":         name,
            "flow_type":    ftype,
            "block_count":  n_blocks,
            "model":        args.model,
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
            "review":       text,
        }, indent=2))
        return

    if args.raw:
        print(text)
        return

    print_review(name, args.model, text)


if __name__ == "__main__":
    main()
