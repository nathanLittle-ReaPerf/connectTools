# connectToolbox.py

Interactive menu launcher for the Amazon Connect Tools suite.

## Dependencies

No pip install required. Uses only Python standard library.

## Usage

```bash
python connectToolbox.py
```

## Navigation

| Key | Action |
|---|---|
| `↑` / `↓` | Move selection |
| `Enter` | Run selected tool |
| `1` – `6` | Jump directly to a tool |
| `q` | Quit |

## Tools

| # | Tool | Script |
|---|---|---|
| 1 | Contacts Handled | `contacts_handled.py` |
| 2 | Contact Inspect | `contact_inspect.py` |
| 3 | Contact Search | `contact_search.py` |
| 4 | Export Flow | `export_flow.py` |
| 5 | Flow to Chart | `flow_to_chart.py` |
| 6 | Log Insights | `log_insights.py` |

After selecting a tool, the menu prompts for each argument one at a time. Optional fields can be left blank to use defaults. When the tool finishes, press Enter to return to the menu.

## Per-tool prompts

**Contacts Handled** — Instance ID, Month (YYYY-MM), Region, Timezone

**Contact Inspect** — Instance ID, Contact ID, Region, Include transcript (y/n)

**Contact Search** — Instance ID, Start date, End date, Region, optional filters (channel, initiation method, queue ID, contact attribute), max contacts, output file

**Export Flow** — Instance ID, Region, optionally list flows first, Flow name, Exact match (y/n), Output file

**Flow to Chart** — Flow JSON file path, Format (html / mermaid / dot), Output file

**Log Insights** — Pick a saved query from the `queries/` folder, fill in any placeholders, optionally customize columns, choose a time range (relative or date range), Region, Log group, Max rows, Output file

## Platform Support

Works on both Windows (local) and Linux (AWS CloudShell). Arrow key handling uses `msvcrt` on Windows and `termios` on Linux — no install required on either platform.

## Notes

- All tools run as subprocesses, so a `sys.exit` in any tool returns cleanly to the menu rather than closing the launcher.
- Export Flow includes an option to list available flows before entering a name, useful when you don't know the exact flow name.
- Flow to Chart defaults to `html` format in the menu (interactive viewer), regardless of the CLI default.
