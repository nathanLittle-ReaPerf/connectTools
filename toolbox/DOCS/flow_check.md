# flow_check.py — Compare Flows Across Regions/Accounts

Verify contact flow parity across regions and AWS accounts with three modes of analysis: quick hash comparison, detailed block diffs, or full flow inventory.

## Usage

### Quick Hash Comparison (Fastest)
```bash
python flow_check.py --flow "Main IVR" --instances abc-123:us-east-1:prod xyz-789:eu-west-1:prod
```

Output:
```
Flow: Main IVR
─────────────────────────────────────────────────────────────────────────────
Label                Region         Hash                Status
─────────────────────────────────────────────────────────────────────────────
prod                 us-east-1      a1b2c3d4e5f6...     ✓ All Match
prod                 eu-west-1      a1b2c3d4e5f6...     ✓ All Match

✓ All flows are identical!
```

### Detailed Block Diffs
```bash
python flow_check.py --flow "Main IVR" --instances abc-123:us-east-1 xyz-789:eu-west-1 --detail
```

Shows hash comparison plus block-by-block differences for mismatched flows.

### Full Flow Inventory
```bash
python flow_check.py --inventory --instances abc-123:us-east-1 xyz-789:eu-west-1
```

Lists all flows across instances with match status:
```
Flow Inventory Across Instances
──────────────────────────────────────────────────────────────────────────────────────────────
Flow Name                                Status            Hashes
──────────────────────────────────────────────────────────────────────────────────────────────
Main IVR                                 ✓ All Match       prod:a1b2c3d4, staging:a1b2c3d4
Transfer to Agent                        ✗ 2 version(s)    prod:xyz789ab, staging:def456gh
...
```

### Export to JSON
```bash
python flow_check.py --flow "Main IVR" --instances abc-123:us-east-1 xyz-789:eu-west-1 --json
python flow_check.py --inventory --instances abc-123:us-east-1 xyz-789:eu-west-1 --json --output inventory.json
```

Output is structured JSON with results array and summary metadata — pipe to `jq` for filtering:
```bash
python flow_check.py --inventory --instances abc-123:us-east-1 xyz-789:eu-west-1 --json | jq '.results[] | select(.match_status == "DIFF")'
```

### Export to CSV
```bash
python flow_check.py --inventory --instances abc-123:us-east-1 xyz-789:eu-west-1 --csv --output flows.csv
```

CSV output includes all flow metadata for use in spreadsheets or other tools.

## Instance Specification Format

```
UUID:REGION[:LABEL]
```

- **UUID** — Connect instance ID (required)
- **REGION** — AWS region (required, e.g., us-east-1)
- **LABEL** — Display label for this instance (optional; defaults to region name)

Examples:
```bash
--instances abc-123:us-east-1:prod xyz-789:eu-west-1:prod
--instances abc-123:us-east-1 xyz-789:us-west-2:staging
```

## Options

| Option | Description |
|--------|-------------|
| `--flow NAME` | Flow name to check (case-insensitive, exact match) |
| `--instances SPEC [SPEC ...]` | Instance specifications (required) |
| `--profile NAME` | AWS named profile (optional) |
| `--detail` | Show detailed block diffs for mismatches |
| `--inventory` | List all flows across instances (instead of checking one flow) |
| `--json` | Output results as JSON (pipe-friendly) |
| `--csv` | Output results as CSV (for spreadsheets/scripting) |
| `--output PATH` | Write output to file (with `--json` or `--csv`; default: stdout) |

## Hash Comparison

The tool generates SHA256 hashes of flow content to quickly identify identical flows:
- **Same hash** — Flows are identical
- **Different hash** — Flows differ (use `--detail` to see block-level diffs)

Hash is 16 characters for readability.

## Use Cases

1. **Verify Prod/Dev Parity** — Ensure Main IVR in prod matches dev
2. **Multi-Region Deployment** — Verify flows are identical across regions after promotion
3. **Audit Instance Configuration** — Inventory all flows with sync status
4. **Troubleshoot Differences** — Find exactly what blocks changed between versions

## Required IAM Permissions

- `connect:ListContactFlows` — list flows in an instance
- `connect:DescribeContactFlow` — fetch flow content

## Exit Codes

- **0** — Success
- **1** — Invalid arguments or AWS errors

## Notes

- Flow names are matched case-insensitively
- Hashes are deterministic — identical flow content always produces the same hash
- The `--detail` mode uses block-level comparison to highlight structural differences
