# security_profile_diff.py

Compare the permission sets of two Amazon Connect security profiles side by side. Shows permissions only in A, only in B, and (optionally) shared by both.

## Usage

```bash
# Human-readable diff
python security_profile_diff.py --instance-id <UUID> --profile-a "Agent" --profile-b "Supervisor" --region us-east-1

# Show shared permissions too
python security_profile_diff.py --instance-id <UUID> --profile-a "Agent" --profile-b "Admin" --all

# Export to CSV
python security_profile_diff.py --instance-id <UUID> --profile-a "Tier 1" --profile-b "Tier 2" --csv diff.csv

# Raw JSON (pipe to jq)
python security_profile_diff.py --instance-id <UUID> --profile-a "Agent" --profile-b "Supervisor" --json | jq '.only_in_b'
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--profile-a` | Name or case-insensitive substring of the first profile (required) |
| `--profile-b` | Name or case-insensitive substring of the second profile (required) |
| `--all` | Also list permissions shared by both profiles |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--csv FILE` | Write diff to a CSV file |
| `--json` | Print JSON to stdout |

## Output

Human-readable output uses colour coding:

- `─` red — permission only in A (A has it, B does not)
- `+` green — permission only in B (B has it, A does not)
- `=` dim — shared by both (shown only with `--all`)

A summary line at the bottom counts each category.

If both profiles have identical permission sets, the tool says so and exits cleanly.

## APIs Used

- `ListSecurityProfiles`
- `ListSecurityProfilePermissions`

## Required IAM

- `connect:ListSecurityProfiles`
- `connect:ListSecurityProfilePermissions`

## CSV Columns

`Permission`, `InA`, `InB`, `Status`

`Status` is one of: `only_in_a`, `only_in_b`, `shared`.
