# Changelog

## 2026-06-12 (continued)
- **Flow Replay** — PNG/ZIP export for flow diagrams. `📸 Generate PNG` renders the Cytoscape.js HTML diagram to a screenshot via Playwright. `📦 Export All Tabs` renders each flow tab separately and bundles them in a ZIP file. Download buttons appear after rendering completes. Setup: `pip install playwright && playwright install chromium`.
- **Flow Replay** — Default log group now saved per-profile with `💾 Save` button. Defaults populated from profile settings; users can update without re-entering contact ID.
- **Queue names** — Contact Search and Contact Investigator now display queue names (not IDs) where available.
- **Unicode fixes** — Windows console encoding issues (box-drawing, arrows) replaced with ASCII equivalents across flowSim tools.
- **Project structure** — Reorganized into `lib/` (shared modules), `connectToolsGui/` (Streamlit GUI), and `toolbox/` (CLI tools). All tools import from lib/ to eliminate duplication.

## 2026-06-12
- **app.py** — Streamlit GUI (local). Two pages: **Credentials** (paste AWS IAM Identity Center Option 2 block → parsed + saved to `~/.aws/credentials`; profile list with add/edit/delete/check; Connect instance ID and region stored per profile in `ct_config`) and **Contact Investigator** (Contact ID lookup with tabbed section output — Overview, Timeline, Lambda, Recordings, Logs — wired directly to `contact_investigator.py` functions). Run with `streamlit run app.py`.

## 2026-06-11
- **flow_analyze.py** — New unified flow analysis tool. Merges `flow_scan` (hard errors: broken refs, dead ends, missing handlers) and `flow_optimize` (rule-based suggestions: UX, reliability, structure, maintainability) into one script. Default runs both passes in a single `DescribeContactFlow` call per flow. `--scan` / `--optimize` flags to restrict to one pass. Replaces both tools in the Flows menu.
- **contact_investigator.py** — New unified contact investigation tool. Consolidates `contact_inspect`, `contact_timeline`, `lambda_tracer`, `contact_recordings`, and `contact_logs` into one script with section flags (`--overview`, `--timeline`, `--lambda`, `--recordings`, `--logs`, `--all`). Shared API calls (DescribeContact, CloudWatch log events, Contact Lens) are made once and reused across sections. Added as "Contact Investigator" at the top of the Contacts group in the toolbox.

## 2026-05-11
- **flow_walk.py** — Step rewind: type `back` or `back N` at any interactive prompt to restore full walker state (SimState + WalkSession) to N steps back. Snapshots taken before each block; rewind propagates across sub-flow boundaries via `_RewindRequest` exception.

## 2026-05-08
- **agent_list.py** — `--status active|inactive|all` filter; toolbox menu updated with status choice prompt.
- **Refactor** — Extracted `ask_time_window()` helper in `connectToolbox.py` to DRY up repeated time window prompts across tools.
- **Docs** — Updated `flowSim/README.md` with `replay_contact.py` workflow and HTML viewer controls.

## 2026-05-07
- **flow_traffic.py** — New tool: flow entry counts (entries + unique contacts, with re-entries tracked separately) and per-contact ordered flow paths from Connect CloudWatch logs. `--flow` filter, `--no-paths`, `--max`, `--csv`, `--json`, `--output`. Added to Flows group in toolbox.
- **flow_sim.py** — New tab for each invocation when the same flow is entered more than once during a simulation.
- **ct_config.py** — Fixed `_maybe_save_log_group` infinite recursion. Added validation to ignore corrupted stored log group values.

## 2026-05-05
- **flow_sim.py HTML viewer** — Arrow key navigation: ↑/↓ steps through the left panel and pans the graph to the selected node without changing zoom level. Scroll wheel now zooms toward the viewport center (keeping the selected node centered) rather than the mouse cursor. Click a step or graph node to fit to that node and its immediate neighbors. Fixed Transfer to Flow ARN lookup — `ContactFlowId` in flow parameters is a full ARN; the simulator now strips to the UUID before cache lookup so sub-flow transfers resolve correctly.

## 2026-05-04
- **replay_contact.py** — New flowSim tool: contact ID → HTML path visualization in one command. Calls `DescribeContact` for the time window, fetches CloudWatch flow logs, reconstructs the contact record (Lambda responses, DTMF inputs, attributes), writes a scenario to `Scenarios/replay_<cid>.json`, and runs `flow_sim.py` to produce `Simulations/replay_<cid>.html`. Added as "Replay contact from logs" to the flowsim interactive menu.
- **Docs** — Added `replay_contact.md`; updated `flow_sim.md` with HTML viewer controls.

## 2026-05-01
- **flow_scan.py** — Added `--csv` output (one row per issue; clean flows included with empty issue fields). Toolbox menu now prompts for CSV output path.
- **connectToolbox.py** — Added `agent_contacts.py` (Agent Contacts) to Agents group and `flow_review.py` (Flow Review AI) to Flows group. Extracted repeated "Save log group?" inline code into `_maybe_save_log_group()` helper. Added `--help` / `-h` flag: prints all tool groups and tool names without launching the interactive menu.
- **contact_search.py** — Added `--offset N` flag for page-by-page iteration through large result sets (client-side slice after full fetch to respect SearchContacts throttle).
- **scenario_from_logs.py** — Attributes grouped by source (Lambda function name vs. flow Set Attribute block) in `--summary` output and `_attr_hints` in scenario JSON. Source tracked via new `attr_sources` field on each contact record.
- **flow_walk.py** — Baseline attribute prompt split into two labeled groups: *Read from incoming state* (must be pre-set) and *Set by this flow* (optional override).
- **Docs** — Added `agent_contacts.md`, `flow_walk.md`, `flowSim/README.md`. Updated `flow_scan.md` with `--csv` flag and example.

## 2026-04-27
- **log_viewer.py** — Interactive TUI timeline viewer for an Amazon Connect contact. Scrollable, filterable timeline of flow blocks, Lambda invocations, contact milestones, and Contact Lens turns. Detail panel for raw event JSON; on-demand Lambda log fetch (`[l]`); live filter bar (`/`); new-contact modal (`[n]`); JSON export (`[e]`). Uses `textual` (auto-installed). Data fetched in background threads.

## 2026-04-23
- **flow_promote.py** — Promote contact flows from Dev to Prod with full ARN remapping (queues, prompts, sub-flows, Lambdas, HOO, quick connects). Interactive sub-flow dependency resolution; topological deploy order; dry-run, backup, and `--publish` support.

## 2026-04-21
- **describe_resource.py** — Look up any Connect resource by ARN, partial ARN, or ID. Returns full describe output for queues, flows, users, routing profiles, phone numbers, and more.

## 2026-03-31
- **flow_walk.py improvements** — Baseline attribute pinning before walk; interactive `SetAttribute` value prompts; Lambda response caching; loop detection with configurable repeat count; flag-for-review system (persists to `for_review/<instance>.json`); auto-save HTML visualization after walk; Play Message / Get Input text wrapping.
- **flow_sim improvements** — Zoom to node on click; faster scroll zoom with manual zoom input; fixed menu corruption on long scenario labels; bare `--html` filenames redirected to `Simulations/` folder.
- **scenario_from_logs** — Added CSV input support for CloudWatch console exports; `--name` flag to override auto-generated scenario name.
- **export_flow_logs** — Default region `us-east-1`; `--contact-id` filter.

## 2026-03-27
- **flow_walk.py** — Interactive step-by-step flow walker. Simulates a contact through a flow in the terminal, prompting for branch choices, attribute values, and Lambda mock outputs.
- **flowSim** toolset — `flow_sim.py` browser-based simulator with arrow-key menus, scenario picker, and HTML visualization. `scenario_from_logs.py` builds scenarios from CloudWatch logs. `export_flow_logs.py` bulk-exports flow logs for offline sim. Output organized into `FlowMaps/`, `Scenarios/`, `Simulations/` folders.
- Docs reorganized into `DOCS/` subfolders per project.

## 2026-03-13
- **lambda_tracer.py** — Trace Lambda invocations for a contact by parsing Connect flow logs, then fetching the actual Lambda CloudWatch logs ±30s around each invocation. `--summary` mode for metadata-only view with on-demand drill-down.
- **routing_profile_audit.py** — Per-profile queue assignments (channel, priority, delay), agent counts, and anomaly detection (no agents, no queues, unassigned queues).
- Toolbox: boto3 auto-upgrade to ≥1.35.0 on startup; openpyxl auto-install.

## 2026-03-12
- **contact_logs.py** — Download CloudWatch flow-execution logs for a contact ID to JSON or plain text.
- **ct_config.py** — Shared config store; saves instance ID, region, profile, and per-instance log group as defaults across tool runs.
- Toolbox: dependency check at startup; tooltips on tool menu items; `ask_date()` with format validation and auto-normalization; data-driven tool runner pattern.
- **contact_inspect.py** — Migrated voice Contact Lens to `ListRealtimeContactAnalysisSegmentsV2`.

## 2026-03-11
- **contact_recordings.py** — S3 locations and presigned download URLs for voice recordings, chat transcripts, and Contact Lens analysis files (original and redacted). Auto-discovers bucket names from `ListInstanceStorageConfigs`.

## 2026-03-05
- Toolbox: mintty / Git Bash on Windows fully fixed (`winpty` wrapper, `_input()` using `sys.stdin.readline()`, UTF-8 stdout, scroll separator instead of ANSI clear, `..` escape to go back from any prompt).
- Added `connectToolbox` shell wrapper script for one-command launch on Windows.
- `.gitattributes` to enforce LF line endings on Python scripts and shell wrappers.

## 2026-03-04
- **contact_search.py** — `SearchContacts` wrapper with CSV/JSON export; filters by channel, queue, agent, initiation method, custom attributes. Handles 0.5 TPS throttle with sleep between pages.
- **cid_journey.py** — Cytoscape.js caller journey map from a `CID_Search` xlsx export.
- Toolbox: run logging to `~/logs/connecttools.log`; ad hoc CloudWatch Logs Insights query input.

## 2026-03-03
- **Initial release** — `connectToolbox.py` interactive menu launcher; `contacts_handled.py`; `contact_inspect.py`; `export_flow.py`; `flow_to_chart.py` (Mermaid / HTML / DOT); `flow_scan.py`; `flow_attr_search.py`; `log_insights.py`; `instance_snapshot.py` + `ct_snapshot.py`; `agent_list.py`; `security_profile_diff.py`; `contact_diff.py`; `contact_timeline.py`; `flow_compare.py`; `flow_optimize.py`; `flow_usage.py`; `orphaned_resources.py`; `phone_numbers.py`.
