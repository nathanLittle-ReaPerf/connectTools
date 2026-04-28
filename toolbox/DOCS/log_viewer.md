# log_viewer.py

Interactive TUI version of `contact_timeline.py`. Launches a full-screen terminal viewer showing a scrollable, filterable, drill-down timeline of flow blocks, Lambda invocations, contact milestones, and Contact Lens turns for a single Amazon Connect contact. Data is fetched in background threads so the UI stays responsive throughout.

## Dependencies

Requires `textual>=0.80.0,<6.3.0`. Auto-installed by `connectToolbox.py` on first run, or manually:

```bash
pip install 'textual>=0.80.0,<6.3.0' --user
```

## Usage

```bash
# Load a specific contact on startup
python log_viewer.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Start the TUI first, enter contact ID interactively with [n]
python log_viewer.py --instance-id <UUID> --region us-east-1

# Override auto-discovered log group
python log_viewer.py --instance-id <UUID> --contact-id <UUID> --log-group /aws/connect/my-instance
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--contact-id` | Contact UUID to load on startup (optional — enter via `[n]` in the TUI) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--log-group` | Override the auto-discovered Connect CloudWatch log group |

## Key Bindings

| Key | Action |
|---|---|
| `↑` / `↓` | Navigate rows |
| `Enter` | Toggle the detail panel for the selected row |
| `/` | Open filter bar — live-filters across kind, label, and detail text |
| `Escape` | Dismiss detail panel → clear filter → close filter bar |
| `l` | Fetch Lambda execution logs for the selected `LAMBDA` row (async, cached) |
| `n` | Open a modal to load a different contact ID without restarting |
| `e` | Export the full timeline JSON to `~/.connecttools/log_viewer/<cid>_timeline.json` |
| `q` | Quit |

## Output

The TUI shows four event kinds in a colour-coded table:

| Kind | Colour | Source |
|---|---|---|
| `CONTACT` | Bold | `DescribeContact` milestones (initiated, queue, agent, disconnected) |
| `FLOW` | Plain | CloudWatch flow log blocks (Play prompt, Check attribute, Set queue, etc.) |
| `LAMBDA` | Yellow | Lambda invocations parsed from flow logs |
| `LENS` | Dim | Contact Lens transcript turns (voice or chat) |

**Detail panel** (`Enter`) — shows the raw source dict for the selected row. For `LAMBDA` rows, also shows the fetched Lambda log lines once `[l]` has been pressed.

**Filter bar** (`/`) — type to live-filter events. Matches against kind, label, and detail text. Press `Escape` to clear.

**Export** (`e`) — writes the same JSON document format as `contact_timeline.py --output`.

## Key Behaviours

- **Auto-discovers log group** from `DescribeInstance` → instance alias. Falls back to `ct_config` cache. Can be overridden with `--log-group`.
- **Background fetch** — all AWS API calls run in worker threads. The TUI header renders immediately after `DescribeContact` completes; flow logs and Contact Lens fetch in the background.
- **Contact Lens** — fetched eagerly on load if the contact is under 24 hours old and the channel is `VOICE` or `CHAT`. Silently skipped if expired or unsupported.
- **Lambda logs** — not fetched on initial load. Press `[l]` on any `LAMBDA` row to fetch `/aws/lambda/<function-name>` logs within ±30 seconds of the invocation timestamp. Results are cached; pressing `[l]` again re-opens the detail panel from cache.
- **New contact** (`[n]`) — resets all state (timeline, filter, lambda cache) and re-runs the full fetch for the new contact ID, without restarting the process.

## Required IAM Permissions

```
connect:DescribeContact
connect:DescribeInstance
connect:DescribeQueue
connect:DescribeUser
connect:ListRealtimeContactAnalysisSegments
logs:FilterLogEvents   (on /aws/connect/<instance-alias>)
logs:FilterLogEvents   (on /aws/lambda/<function-name>  — for [l])
```

## APIs Used

| API | Purpose |
|---|---|
| `DescribeContact` | Fetch contact milestones and time window |
| `DescribeInstance` | Resolve instance alias for log group auto-discovery |
| `DescribeQueue` | Resolve queue ID to name |
| `DescribeUser` | Resolve agent ID to name |
| `FilterLogEvents` (Connect) | Fetch flow-execution log entries for the contact |
| `FilterLogEvents` (Lambda) | Fetch Lambda execution logs on demand (`[l]`) |
| `ListRealtimeContactAnalysisSegmentsV2` | Fetch Contact Lens transcript turns |

## Changelog

| Version | Change |
|---|---|
| Initial | TUI timeline viewer with scroll, filter, detail panel, on-demand Lambda logs, export, and new-contact modal |
