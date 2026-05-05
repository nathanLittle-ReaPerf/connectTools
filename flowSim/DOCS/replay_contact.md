# replay_contact.py — Replay a Real Contact as an HTML Flow Visualization

Fetches CloudWatch flow logs for a real contact, reconstructs exactly what happened (Lambda responses, DTMF inputs, attribute values set by the flow), and runs `flow_sim.py` to produce an HTML graph of the path taken — all in one command.

The same result could be achieved by running `export_flow_logs.py` → `scenario_from_logs.py` → `flow_sim.py` manually. `replay_contact.py` automates that pipeline.

Requires the flow cache (run `flow_map.py` first) and CloudWatch flow logs to be within retention (usually 30 days).

---

## Usage

```bash
python replay_contact.py --instance-id <UUID> --contact-id <UUID> --region us-east-1
```

Output files:
- `flowSim/Scenarios/replay_<cid8>.json` — scenario built from real log data
- `flowSim/Simulations/replay_<cid8>.html` — HTML path visualization

### Options

| Option | Description |
|---|---|
| `--instance-id UUID` | Connect instance UUID (required) |
| `--contact-id UUID` | Contact ID to replay (required) |
| `--region REGION` | AWS region |
| `--profile NAME` | Named AWS profile for local use |
| `--html FILE` | Override HTML output path |
| `--no-html` | Skip HTML; write scenario file only |
| `--log-group NAME` | Override log group (default: auto-discovered from instance alias) |

---

## How it works

1. **DescribeContact** — fetches `InitiationTimestamp` and `DisconnectTimestamp` to set the CloudWatch query window.
2. **FilterLogEvents** — fetches all flow log events for the contact ID from the Connect log group (`/aws/connect/<alias>`).
3. **Reconstruct** — parses events to extract:
   - Entry flow name
   - Contact attributes set by flow blocks and Lambda returns (with source tracked separately)
   - Lambda invocation ARNs, results, and `$.External.*` values returned
   - DTMF / GetUserInput presses
   - Hours-of-operation and staffing check outcomes
4. **Write scenario** — saves the reconstructed data as a `flow_sim.py`-compatible scenario JSON to `flowSim/Scenarios/replay_<cid8>.json`.
5. **Simulate** — runs `flow_sim.py` with the scenario to produce the HTML visualization.

---

## What the scenario captures

| Field | Source |
|---|---|
| `attributes` | Last value seen per key across all `UpdateContactAttributes` blocks |
| `lambda_mocks` | Per-function: last result status + all `$.External.*` values returned |
| `dtmf_inputs` | Per "flow / block" key: the digit(s) the caller pressed |
| `hours_mocks` | Per HOO ID: whether the check resolved to in-hours |
| `staffing_mocks` | Per queue ID: whether the check resolved to staffed |
| `_attr_hints` | Source annotation per attribute (`[flow]` or `[lambda-name]`) |

---

## IAM Permissions

```
connect:DescribeContact
connect:DescribeInstance
logs:FilterLogEvents on /aws/connect/<instance-alias>
```

---

## Limitations

- **Log retention** — CloudWatch flow logs are typically retained for 30 days. Contacts older than that will show no events.
- **Flow cache required** — `flow_sim.py` reads from the local cache. Run `flow_map.py` first.
- **Transfer chains** — if the contact transferred across multiple flows, all flow IDs in the chain must be in the cache for the simulation to follow them.
- **Entry flow detection** — the entry flow is taken from the first `ContactFlowName` seen in the logs. If logs are sparse, you will be prompted to enter the flow name manually.

---

## Changelog

| Version | Change |
|---|---|
| 2026-05-04 | Initial release |
