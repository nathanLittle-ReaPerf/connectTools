# contact_diff.py

Side-by-side comparison of two Amazon Connect contacts. Compares core metadata (channel, initiation method, queue, agent, duration, timestamps, disconnect reason, customer endpoint), custom contact attributes, and Contact Lens summaries (status, turn count, sentiment, categories, issues, post-contact summary). Matching fields are dimmed; differing fields are highlighted with a red/green indicator. By default, attributes are shown only when they differ between the two contacts.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --region us-east-1
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--contact-id-a` | First contact UUID — labelled A in the output (required) |
| `--contact-id-b` | Second contact UUID — labelled B in the output (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--all-attrs` | Show all contact attributes, not just those that differ |
| `--json` | Emit a single JSON document containing raw contact data and the full diff table |

### Examples

```bash
# Human-readable side-by-side diff
python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID>

# Show all attributes (not just differing ones)
python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --all-attrs

# Raw JSON output
python contact_diff.py --instance-id <UUID> --contact-id-a <UUID> --contact-id-b <UUID> --json
```

## Output

**Human-readable** — three sections separated by rule lines:

```
  ──────────────────────────────────────────────────────────────────────────
  CONTACT DIFF
  ──────────────────────────────────────────────────────────────────────────
  A                       <contact-id-a>
  B                       <contact-id-b>

  2 field(s) differ  (8/10 match)

  ── CORE ──
  Channel                 VOICE                    ✓  VOICE
  Queue                   Billing                  ✗  Support
  ...

  ── ATTRIBUTES ──
  accountType             gold                     ✗  silver

  ── CONTACT LENS ──
  Customer sentiment      POSITIVE (+3 =1 -0)      ✓  POSITIVE (+2 =2 -0)
```

Matching rows are dimmed (grey). Differing rows show values in normal white. `[absent]` indicates a key present in one contact but not the other.

**JSON (`--json`):** Single document with keys `contact_a`, `contact_b`, `attributes_a`, `attributes_b`, `lens_a`, `lens_b`, `names_a`, `names_b`, and `diff` (containing `core`, `attributes`, `lens` arrays of `{field, a, b, match}` objects). Datetimes are ISO strings.

## Key Behaviours

- Both contacts are fetched with `DescribeContact` up-front — if either fails the tool exits immediately with a clear error rather than failing mid-comparison.
- Queue and agent names are resolved via `DescribeQueue` and `DescribeUser` for display; IDs are shown as fallback if resolution fails.
- Contact Lens data has a 24-hour retention window. Contacts older than 24 hours show `Expired (>24h)` in the Lens section rather than attempting an API call.
- Contact Lens is channel-aware: voice contacts use segment types `TRANSCRIPT`, `CATEGORIES`, `ISSUES`, `SENTIMENT`; chat/email contacts additionally include `EVENT`, `ATTACHMENTS`, `POST_CONTACT_SUMMARY`.
- In the attributes section, by default only differing attributes are shown. Use `--all-attrs` to see all. A note at the bottom states how many matching attributes are hidden.
- If both contact IDs are the same, a warning is printed (all fields will match).

## Required IAM Permissions

```
connect:DescribeContact
connect:GetContactAttributes
connect:ListRealtimeContactAnalysisSegments
connect:DescribeQueue
connect:DescribeUser
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeContact` | Fetch core metadata for each contact (channel, queue, agent, timestamps, etc.) |
| `GetContactAttributes` | Fetch custom contact attributes for each contact |
| `ListRealtimeContactAnalysisSegmentsV2` | Fetch Contact Lens transcript, sentiment, categories, and issues for each contact |
| `DescribeQueue` | Resolve queue ID to name for display |
| `DescribeUser` | Resolve agent ID to first/last name for display |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: side-by-side diff of core metadata, attributes, and Contact Lens summary; --all-attrs; --json output |
