# agent_list.py

List all agents (users) in an Amazon Connect instance with their username, first/last name, routing profile, hierarchy group, and security profiles. Routing profile, hierarchy group, and security profile names are resolved via describe calls with local caches to minimise API requests. Results can be filtered by username substring or routing profile name, exported to CSV, or output as JSON.

## Dependencies

No pip install required beyond boto3, which is pre-installed in AWS CloudShell.

## Usage

```bash
python agent_list.py --instance-id <UUID> --region us-east-1
```

| Flag | Description |
|---|---|
| `--instance-id` | Amazon Connect instance UUID (required) |
| `--search` | Case-insensitive substring match on username |
| `--routing-profile` | Filter by routing profile name (case-insensitive substring); applied after fetching user details |
| `--region` | AWS region — defaults to CloudShell/session region |
| `--profile` | Named AWS profile for local use |
| `--csv` | Write results to a CSV file at the given path |
| `--json` | Print results as JSON (pipe-friendly) |

### Examples

```bash
# List all agents (table output)
python agent_list.py --instance-id <UUID>

# Search by username substring
python agent_list.py --instance-id <UUID> --search jsmith

# Filter by routing profile name
python agent_list.py --instance-id <UUID> --routing-profile "Basic Routing"

# Export to CSV
python agent_list.py --instance-id <UUID> --csv agents.csv

# JSON output — extract usernames with jq
python agent_list.py --instance-id <UUID> --json | jq '.[].Username'
```

## Output

**Human-readable** — columnar table with Username, FirstName, LastName, RoutingProfile, HierarchyGroup, followed by a count line:

```
Username         FirstName   LastName   RoutingProfile   HierarchyGroup
--------         ---------   --------   --------------   --------------
jsmith           Jane        Smith      Basic Routing    Team A
bjones           Bob         Jones      Tier 2           Team B

2 agent(s).
```

**CSV (`--csv`):** Columns — Username, FirstName, LastName, Email, RoutingProfile, SecurityProfiles, HierarchyGroup, PhoneType, UserId.

**JSON (`--json`):** Array of objects with all columns above as keys.

## Key Behaviours

- Paginates `ListUsers` and filters by username substring before fetching details — avoids describing users that don't match `--search`.
- Routing profile, hierarchy group, and security profile names are resolved with local in-memory caches. Users sharing the same profile incur only one describe call each.
- `--routing-profile` filter is applied client-side after all user details have been fetched; it does not reduce the number of API calls.
- A stderr progress line (`Fetching details... N/total`) is displayed while describing users.
- Security profiles are joined with `; ` in table and CSV output.

## Required IAM Permissions

```
connect:ListUsers
connect:DescribeUser
connect:DescribeRoutingProfile
connect:DescribeUserHierarchyGroup
connect:DescribeSecurityProfile
```

## APIs Used

| API | Purpose |
|---|---|
| `ListUsers` | Paginate all users in the instance, optionally pre-filtered by username substring |
| `DescribeUser` | Fetch routing profile ID, hierarchy group ID, security profile IDs, phone config, and identity info per user |
| `DescribeRoutingProfile` | Resolve routing profile ID to name (cached per ID) |
| `DescribeUserHierarchyGroup` | Resolve hierarchy group ID to name (cached per ID) |
| `DescribeSecurityProfile` | Resolve each security profile ID to name (cached per ID) |

## Changelog

| Version | Change |
|---|---|
| Initial | Core tool: user listing, name resolution with caches, table/CSV/JSON output, --search and --routing-profile filters |
