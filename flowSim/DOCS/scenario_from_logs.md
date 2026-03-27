# scenario_from_logs.py — Scenario Builder from CloudWatch Logs

Turns exported Amazon Connect CloudWatch flow logs into ready-to-use scenario files for `flow_sim.py`. Parses real contact journeys to extract attribute values, Lambda results, DTMF presses, and hours/staffing outcomes — so your test scenarios reflect what actually happens in production.

---

## Usage

```bash
# Parse a CW export and write scenario files for the top 5 most common journeys
python scenario_from_logs.py contacts.json

# Multiple log files
python scenario_from_logs.py logs/march_*.json

# Single contact by ID
python scenario_from_logs.py contacts.json --contact-id <UUID>

# Merge all contacts into one representative scenario
python scenario_from_logs.py contacts.json --merge

# Top 10 journeys instead of default 5
python scenario_from_logs.py contacts.json --top 10

# Write scenarios to a specific directory
python scenario_from_logs.py contacts.json --out-dir ./scenarios/

# Scrub PII from attribute values before writing
python scenario_from_logs.py contacts.json --anonymize

# Preview — list contacts and journeys without writing files
python scenario_from_logs.py contacts.json --list

# Summary — show attribute key distributions and Lambda ARNs
python scenario_from_logs.py contacts.json --summary

# JSON output of parsed contact data (pipe-friendly)
python scenario_from_logs.py contacts.json --json | jq '.contacts[0].attributes'
```

---

## Options

| Option | Description |
|---|---|
| `LOG_FILE [...]` | One or more CloudWatch export files. |
| `--out-dir DIR` | Output directory. Default: `flowSim/Scenarios/`. |
| `--merge` | Merge all contacts into one scenario (most common value per attribute). |
| `--top N` | Write scenarios for top N journeys by frequency. Default: 5. |
| `--contact-id UUID` | Extract a single contact. Accepts UUID prefix. |
| `--archetypes` | Generate named scenario files grouped by decision-attribute profile (e.g. `No_Account`, `Premium`, `Auth_Failed`). Requires `--instance-id`. |
| `--instance-id UUID` | Connect instance UUID. Required for `--archetypes` to load the flow map cache. |
| `--anonymize` | Replace PII-looking values with safe placeholders. |
| `--list` | List contacts with journey summaries; don't write files. |
| `--summary` | Print attribute key distributions and Lambda ARNs. |
| `--json` | Print parsed contact data as JSON to stdout. |

---

## Input formats

The tool auto-detects which CloudWatch export format you have:

**`aws logs filter-log-events` output** (most common):
```bash
aws logs filter-log-events \
    --log-group-name /aws/connect/<instance-alias> \
    --start-time <epoch-ms> \
    --end-time <epoch-ms> \
    --output json > contacts.json
```
Produces a JSON object with an `"events"` array — handled automatically.

**CloudWatch Logs Insights export** — a JSON object with a `"message"` field per line. Handled automatically.

**JSON Lines** — one raw Connect log event JSON per line. Handled automatically.

---

## What is extracted per contact

| Data | Source in log |
|---|---|
| Contact attributes set | `UpdateContactAttributes` block parameters |
| Lambda results + `$.External.*` | `InvokeExternalResource` block results |
| DTMF presses | `GetUserInput` block results |
| Hours-of-operation outcomes | `CheckHoursOfOperation` block results |
| Queue staffing outcomes | `CheckStaffing` block results |
| ANI / DNIS | `CustomerEndpoint` / `SystemEndpoint` fields |
| Flow sequence | `ContactFlowName` across all events for the contact |

---

## Output files

### Per-journey mode (default)

Writes one file per unique journey (by flow sequence), ordered by frequency:

```
scenario_01_Main_IVR.json          ← most common journey
scenario_02_Main_IVR.json          ← second most common
...
```

### Merged mode (`--merge`)

```
scenario_merged_<N>_contacts.json
```

Uses the most common value for each attribute across all contacts. The `_attr_hints` block lists all unique values seen.

### Single contact (`--contact-id`)

```
scenario_contact_<first-8-chars>.json
```

---

## Anonymization

`--anonymize` replaces values that look like PII with safe placeholders:

| Pattern | Replacement |
|---|---|
| Phone number (7+ digits) | `+10000000000` or `PHONE_REDACTED` |
| Email address | `user@example.com` |
| UUID | `00000000-0000-0000-0000-000000000000` |
| All-digit string (6–20 chars) | `1234567890` |

Non-PII values (e.g. `"premium"`, `"billing"`) are passed through unchanged.

---

## Journey grouping

Contacts are grouped by their **flow sequence** — the ordered list of distinct flows traversed (e.g. `Main IVR → Auth Flow → Billing Flow`). Contacts that went through the same flows in the same order are considered the same journey type, regardless of which branches they took within each flow.

The `--top N` flag writes scenarios for the N most frequently occurring journey types. For each group, the most recently seen contact is used as the representative.

---

## Archetype mode

`--archetypes` generates named scenario files based on how real contacts actually answered the key decision points in your flows.

It works by:
1. Loading the flow map cache (`~/.connecttools/flows/<instance-id>/`) to find every **decision attribute** — attributes used in `Compare`/`CheckAttribute` blocks — along with the comparison values your flows actually test against.
2. Profiling each contact by the last value set for each decision attribute.
3. Grouping contacts by their combined profile (fingerprint). For each group, the most recent contact is used as the representative.
4. Naming each group using a tiered heuristic:

| Attribute value | Name component |
|---|---|
| Empty / `"null"` | `No_{Attr}` |
| Contains `"error"` / `"fail"` / `"invalid"` | `{Attr}_Failed` |
| `"true"` / `"yes"` / `"1"` | `{Attr}` |
| Anything else | PascalCase of value (truncated to 24 chars) |

5. Printing a **coverage report** that shows which known attribute+value combinations from the flow map had no matching contacts — these are the gaps you may want to hand-craft.

### Archetype output files

```
archetype_01_Premium.json
archetype_02_No_AccountNumber.json
archetype_03_Auth_Failed.json
...
```

### Example

```bash
# 1. Map the instance first (one-time; skip if cache already exists)
python flow_map.py --instance-id <UUID> --region us-east-1

# 2. Export CloudWatch flow logs
aws logs filter-log-events \
    --log-group-name /aws/connect/my-instance \
    --start-time $(date -d '4 hours ago' +%s000) \
    --output json > recent_contacts.json

# 3. Build named archetypes
python scenario_from_logs.py recent_contacts.json \
    --archetypes --instance-id <UUID>

# 4. Simulate a specific archetype
python flow_sim.py \
    --instance-id <UUID> \
    --flow "Main IVR" \
    --scenario Scenarios/archetype_01_Premium.json
```

**How many logs do you need?** A few hundred contacts from a normal business day is usually enough to cover the common paths. The coverage report tells you what's missing — for low-frequency paths like error cases, you may need to hand-edit or source a targeted log window.

---

## Example: build and use a scenario

```bash
# 1. Export a few hours of Connect flow logs
aws logs filter-log-events \
    --log-group-name /aws/connect/my-instance \
    --start-time $(date -d '4 hours ago' +%s000) \
    --output json > recent_contacts.json

# 2. See what's in the logs
python scenario_from_logs.py recent_contacts.json --list
python scenario_from_logs.py recent_contacts.json --summary

# 3. Generate scenario files
python scenario_from_logs.py recent_contacts.json

# 4. Simulate with flow_sim.py
python flow_sim.py \
    --instance-id <UUID> \
    --flow "Main IVR" \
    --scenario Scenarios/scenario_01_Main_IVR.json
```
