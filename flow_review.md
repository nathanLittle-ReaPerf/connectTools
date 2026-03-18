# flow_review.py

AI-powered deep analysis of an Amazon Connect contact flow. Sends a structured flow summary to the Claude API and returns plain-English optimization recommendations covering caller experience, reliability, structure, and AWS Connect best practices. Complements `flow_optimize.py` (rule-based) with intent-level analysis that rules can't catch.

## Dependencies

```bash
pip install anthropic --user
```

Requires `ANTHROPIC_API_KEY` environment variable.

## Usage

```bash
python flow_review.py FLOW_JSON
```

| Flag | Description |
|---|---|
| `FLOW_JSON` | Exported flow JSON file (from `export_flow.py` or raw flow content) |
| `--model MODEL` | Claude model (default: `claude-opus-4-6`) |
| `--api-key KEY` | Anthropic API key (default: `ANTHROPIC_API_KEY` env var) |
| `--max-tokens N` | Max response tokens (default: 2048) |
| `--json` | Print full JSON output including token usage |
| `--raw` | Print only raw model text with no formatting |

### Examples

```bash
# Deep review with Opus (default)
python flow_review.py Main_IVR.json

# Faster/cheaper with Sonnet
python flow_review.py Main_IVR.json --model claude-sonnet-4-6

# JSON output with token usage
python flow_review.py Main_IVR.json --json | jq '.review'
```

## Output

```
  ────────────────────────────────────────────────────────────────────────
  FLOW REVIEW   Main IVR
  Model: claude-opus-4-6
  ────────────────────────────────────────────────────────────────────────

  ## 1. Caller Experience (UX)

  **Main Menu (GetUserInput):** The menu offers 6 options with no retry
  path. Consider reducing to 3–4 options and adding a NoMatch branch
  that repeats the prompt up to 3 times before escalating.

  **Welcome Message (PlayPrompt):** The welcome text is 42 words — aim
  for under 20 words to reduce time-to-menu.

  ...
```

## Key Behaviours

- **Flow summary built before API call** — the flow JSON is converted to a structured text representation (block names, types, key parameters, transitions). This gives Claude the context it needs without sending raw JSON.
- **BFS traversal** — blocks are presented in call-order (from StartAction), making the summary readable for the model.
- **Recommended workflow**: run `flow_optimize.py` first for instant rule-based feedback, then `flow_review.py` for deeper architectural analysis.
- **Token cost**: a typical 25-block flow uses ~1,500–2,500 input tokens with Opus.

## Recommended Workflow

```bash
# 1. Export the flow
python export_flow.py --instance-id <UUID> --name "Main IVR" --output main_ivr.json

# 2. Rule-based check (free, instant)
python flow_optimize.py main_ivr.json

# 3. AI deep review (uses API tokens)
python flow_review.py main_ivr.json
```

## Changelog

| Version | Change |
|---|---|
| Initial | Claude API deep review; BFS flow summary; Opus default; JSON/raw output modes |
