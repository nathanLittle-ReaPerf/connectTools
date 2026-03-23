# connectToolbox.py

Interactive menu launcher for the Amazon Connect Tools suite.

## Dependencies

No pip install required. Uses only Python standard library.

## Usage

**AWS CloudShell (recommended):**
```bash
./connectToolbox.py
```

**Local Windows (Cmder / Git Bash):**
```bash
./connectToolbox
```

The `connectToolbox` wrapper script (no `.py` extension) invokes `winpty` automatically. Running `python connectToolbox.py` directly on Windows in Cmder/Git Bash will display the menu but silently ignore all keyboard input — see [Troubleshooting](#troubleshooting) below.

## Navigation

| Key | Action |
|---|---|
| `↑` / `↓` | Move selection |
| `Enter` | Run selected tool |
| `1` – `9` | Jump directly to an item by number |
| `q` | Quit / go back |

The menu is two levels: pick a **group**, then pick a **tool** within it. Press `q` in the tool submenu to return to the group list.

## Tool Groups

### Contacts
| # | Tool | Script |
|---|---|---|
| 1 | Contacts Handled | `contacts_handled.py` |
| 2 | Contact Inspect | `contact_inspect.py` |
| 3 | Contact Timeline | `contact_timeline.py` |
| 4 | Contact Diff | `contact_diff.py` |
| 5 | Contact Search | `contact_search.py` |
| 6 | Contact Recordings | `contact_recordings.py` |
| 7 | Contact Logs | `contact_logs.py` |
| 8 | Lambda Tracer | `lambda_tracer.py` |
| 9 | Lambda Errors | `lambda_errors.py` |

### Flows
| # | Tool | Script |
|---|---|---|
| 1 | Flow Scan | `flow_scan.py` |
| 2 | Flow Attr Search | `flow_attr_search.py` |
| 3 | Flow Optimize | `flow_optimize.py` |
| 4 | Flow Usage | `flow_usage.py` |
| 5 | Flow Compare | `flow_compare.py` |
| 6 | Orphaned Resources | `orphaned_resources.py` |
| 7 | Export Flow | `export_flow.py` |
| 8 | Flow to Chart | `flow_to_chart.py` |

### Log Insights
| # | Tool | Script |
|---|---|---|
| 1 | Log Insights | `log_insights.py` |
| 2 | CID Journey | `cid_journey.py` |

### Agents
| # | Tool | Script |
|---|---|---|
| 1 | Agent Activity | `agent_activity.py` |
| 2 | Agent List | `agent_list.py` |
| 3 | Routing Profile Audit | `routing_profile_audit.py` |
| 4 | Security Profile Diff | `security_profile_diff.py` |

### Instance
| # | Tool | Script |
|---|---|---|
| 1 | Instance Snapshot | `instance_snapshot.py` |
| 2 | Phone Numbers | `phone_numbers.py` |

After selecting a tool, the menu prompts for each argument one at a time. Optional fields can be left blank to use defaults. When the tool finishes, press Enter to return to the tool submenu.

## Per-tool prompts

**Contacts Handled** — Instance ID, Month (YYYY-MM), Region, Timezone

**Contact Inspect** — Instance ID, Contact ID, Region, Include transcript (y/n)

**Contact Search** — Instance ID, Start date, End date, Region, optional filters (channel, initiation method, queue ID, contact attribute), max contacts, output file

**Contact Recordings** — Instance ID, Contact ID, Region, URL expiry (seconds, default 3600)

**Flow Scan** — Instance ID, Region, Flow name (or blank for all), Flow type filter, Show per-block detail (y/n)

**Flow Attr Search** — Attribute name, Source (local file(s) / instance flow / all flows), then path(s) or Instance ID + name/type filter, Show per-block detail (y/n), Exact case match (y/n)

**Flow Optimize** — Source (local file / instance flow / all flows), then flow path or Instance ID + name/type filter


**Flow Usage** — Instance ID, Region, Count by (contacts / invocations), Flow name filter, Time window (7d default / 24h / 30d / custom), Output CSV

**Flow Compare** — Left (older) flow JSON file path, Right (newer) flow JSON file path

**Orphaned Resources** — Instance ID, Region, Verify Lambda ARNs (y/n), Output CSV

**Export Flow** — Instance ID, Region, optionally list flows first, Flow name, Exact match (y/n), Output file

**Flow to Chart** — Flow JSON file path, Format (html / mermaid / dot), Output file

**Log Insights** — Pick a saved query from the `queries/` folder, fill in any placeholders, optionally customize columns, choose a time range (relative or date range), Region, Log group, Max rows, Output file

**Agent Activity** — Instance ID, Region, Named period or date range, optional agent login filter, Output CSV

**Contact Timeline** — Instance ID, Contact ID, Region, Include transcript (y/n)

**Contact Diff** — Instance ID, Contact ID A, Contact ID B, Region, Show all attributes (y/n)

**Contact Logs** — Instance ID, Contact ID, Region, Output file

**Lambda Tracer** — Instance ID, Contact ID, Region, Summary only (y/n), Output file

**Lambda Errors** — Instance ID, Function name (substring), Region, Time window (last N / start+end / period), Output CSV

**Agent List** — Instance ID, Region, optional username search (substring), optional routing profile filter, Output CSV

**Routing Profile Audit** — Instance ID, Region, optional name filter, Output CSV

**Security Profile Diff** — Instance ID, Profile A name, Profile B name, Show shared permissions (y/n), Output CSV

**Instance Snapshot** — Instance ID, Region (refreshes or shows existing snapshot)

**Phone Numbers** — Instance ID, Region, optional flow name filter, unassigned only (y/n), Output CSV

## Logging

Every tool run is appended to `~/logs/connecttools.log`. The `logs/` directory is created automatically on first run.

Each line contains:

```
2026-03-04 14:32:01  contact_search        exit=0             12.3s  --instance-id f79da75c --start 2026-03-01 --end 2026-03-02
2026-03-04 14:45:18  contact_inspect       exit=1  ERROR      0.4s   --instance-id f79da75c --contact-id abc123
```

Fields: timestamp, tool name, exit code (non-zero runs are flagged `ERROR`), duration, full argument list.

## Platform Support

Works on both Linux (AWS CloudShell) and Windows (local). Arrow key handling uses `msvcrt` on Windows and `termios` on Linux — no install required on either platform.

## Troubleshooting

### Input silently ignored on Windows (Cmder / Git Bash)

**Symptoms:**
- Menu displays correctly, but typing a number and pressing Enter does nothing
- Characters may echo to the screen but the selection never registers
- Arrow keys move the terminal cursor around the menu instead of navigating it
- Running `python3 -c "import sys; print(sys.stdin.isatty())"` prints `False`

**Cause:** In Cmder and Git Bash (mintty), Python's `sys.stdin` is a pipe rather than a real TTY. The menu prompt appears (stdout works fine) but reads from stdin block indefinitely waiting for data that never arrives through the pipe.

**Fix:** Use the `connectToolbox` wrapper script instead of invoking Python directly:
```bash
./connectToolbox        # wraps with winpty automatically
```

`winpty` bridges mintty's PTY to the Windows console API that Python expects. It ships with **Git for Windows** — if you have Git Bash you already have it. Verify with `which winpty`.

## Adding to PATH

### AWS CloudShell

Add to `~/.bashrc` (persists across CloudShell sessions):

```bash
alias connecttools='python ~/connectTools/connectToolbox.py'
```

Then reload:
```bash
source ~/.bashrc
```

Run from anywhere with `connecttools`.

---

### Local — Cmder / Git Bash (Windows)

Add to `~/.bashrc`:

```bash
alias connecttools='winpty python /c/Users/nathan.littlerea/workStuffs/connectTools/connectToolbox.py'
```

Then reload:
```bash
source ~/.bashrc
```

Run from anywhere with `connecttools`.

---

### Local — PowerShell or cmd

Create a `.bat` file in a folder that's already on your Windows `PATH`
(e.g. `C:\Users\nathan.littlerea\bin\connecttools.bat`):

```bat
@echo off
winpty python C:\Users\nathan.littlerea\workStuffs\connectTools\connectToolbox.py %*
```

Run from anywhere with `connecttools`.

## Notes

- All tools run as subprocesses, so a `sys.exit` in any tool returns cleanly to the menu rather than closing the launcher.
- Export Flow includes an option to list available flows before entering a name, useful when you don't know the exact flow name.
- Flow to Chart defaults to `html` format in the menu (interactive viewer), regardless of the CLI default.
