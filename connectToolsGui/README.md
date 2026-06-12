# connectToolsGui — Streamlit GUI for Amazon Connect

A browser-based local interface for the connectTools suite. Provides pages for credential management, contact search, contact investigation, flow analysis, Lambda error tracking, flow replay visualization, and log queries.

## Quick Start

```bash
# From this directory (connectToolsGui/)
pip install -r requirements.txt
streamlit run app.py
```

The app opens at `http://localhost:8501`.

## Pages

- **🔑 Credentials** — Paste AWS IAM Identity Center Option 2 blocks, manage per-profile metadata (instance ID, region, log group). Inline credential refresh with individual fields (auto-opens when expired).
- **🔎 Contact Search** — Date range + channel/queue/method filters; selectable results; click to investigate or compare two contacts.
- **🔍 Contact Investigator** — Deep dive on a contact: overview, timeline, Lambda invocations, recordings, logs. Tabbed results.
- **↔️ Contact Diff** — Side-by-side comparison of two contacts: core metadata, attributes, Contact Lens analysis.
- **⚡ Lambda Errors** — Aggregate Lambda errors over a time window; search Connect flow logs for affected contacts.
- **🔬 Flow Analyze** — Scan flows for hard errors (broken refs, dead ends) and optimization suggestions (UX, reliability, structure).
- **🎬 Flow Replay** — Reconstruct the exact path a real contact took through your flows as an interactive HTML visualization.
- **📊 Log Insights** — CloudWatch Logs Insights query editor with live placeholder detection, saved queries, log group discovery, results as dataframe + Excel export.

## Profile Data Model

Profiles are stored in `~/.connecttools/ct_config.json` under `gui_profiles[profile_name]`:

```json
{
  "display_name": "Production Admin",
  "instance_id":  "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "region":       "us-east-1",
  "log_group":    "/aws/connect/my-instance",
  "added_at":     "2026-06-12T..."
}
```

Each profile is a complete AWS context. Switching profiles in the sidebar updates all page defaults.

## Dependencies

- **boto3** — AWS API calls
- **streamlit** — web framework
- **pandas** — dataframe rendering
- **openpyxl** — Excel export (optional; Log Insights falls back without it)

Shared modules are in `../lib/` (one level up):
- `contact_investigator.py` — unified contact investigation
- `contact_search.py` — SearchContacts wrapper
- `flow_analyze.py` — flow error scanner + optimizer
- `lambda_errors.py` — Lambda error aggregator
- `contact_diff.py` — contact side-by-side diff
- `ct_config.py` — profile metadata storage
- `ct_snapshot.py` — instance resource cache

The GUI also integrates with `../flowSim/` for flow replay visualization.

## Troubleshooting

**"Module not found"** — Make sure you're running from this directory (`connectToolsGui/`), and `python -m pip list` shows `streamlit` installed.

**Port 8501 already in use** — Streamlit uses port 8501 by default. To use a different port:
```bash
streamlit run app.py --server.port 8502
```

**Credentials not persisting** — Check that `~/.connecttools/` is readable/writable:
```bash
ls -la ~/.connecttools/ct_config.json
```

## Development

The app is organized into pages:
- `page_credentials()` — profile CRUD + inline refresh
- `page_contact_search()` — SearchContacts + multi-row selection
- `page_contact_investigator()` — detailed contact analysis
- `page_contact_diff()` — field-by-field comparison
- `page_lambda_errors()` — error aggregation + blast radius
- `page_flow_analyze()` — scan + optimize
- `page_flow_replay()` — scenario reconstruction + visualization
- `page_log_insights()` — Logs Insights query editor

Key Streamlit patterns used:
- **Key counters** for widget resets (increment counter in key name to force re-render)
- **Session state** for profile switching (detect changes, clear page state)
- **Multiselect dataframes** with quick-link buttons to other pages

## License

Same as connectTools (parent project).
