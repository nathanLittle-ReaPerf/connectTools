# routing_profile_audit.py

Audit every routing profile in an Amazon Connect instance. Shows per-profile queue assignments (channel, priority, delay) and agent count, then flags anomalies: profiles with no agents, profiles with no queues, and queues not assigned to any profile.

## Usage

```bash
# Human-readable — all profiles
python routing_profile_audit.py --instance-id <UUID> --region us-east-1

# Filter to profiles whose name contains a substring
python routing_profile_audit.py --instance-id <UUID> --name "Tier 2"

# Export to CSV
python routing_profile_audit.py --instance-id <UUID> --csv audit.csv

# Raw JSON (pipe to jq)
python routing_profile_audit.py --instance-id <UUID> --json | jq '.anomalies'
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--name` | Case-insensitive substring filter on routing profile name |
| `--csv FILE` | Write results to a CSV file |
| `--json` | Print JSON to stdout |

## APIs Used

- `ListRoutingProfiles`
- `ListRoutingProfileQueues`
- `ListQueues`
- `ListUsers`
- `DescribeUser`

## Required IAM

- `connect:ListRoutingProfiles`
- `connect:ListRoutingProfileQueues`
- `connect:ListQueues`
- `connect:ListUsers`
- `connect:DescribeUser`

## Anomalies Detected

| Anomaly | Meaning |
|---|---|
| Profile with 0 agents | No agents will receive contacts routed to this profile's queues |
| Profile with no queues | Profile exists but has nothing to route to |
| Queue not in any profile | Queue is configured but unreachable via routing |

## CSV Columns

`RoutingProfile`, `RoutingProfileId`, `Agents`, `Channel`, `Queue`, `QueueId`, `Priority`, `Delay`
