# flow_attr_search.py — Flow Attribute Search

Search one or all contact flows for every place a contact attribute is **set**, **checked**, or **referenced**.

## Usage

```bash
# Search a local exported flow file
python flow_attr_search.py --attribute myAttr Main_IVR.json

# Search multiple local files
python flow_attr_search.py --attribute myAttr *.json

# Search a single live flow by name
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --name "Main IVR" --region us-east-1

# Search all flows in the instance (summary table)
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --all

# Bulk search with per-block detail
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --all --detail

# Filter by flow type
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --all --type CONTACT_FLOW

# Exact-case match
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --all --exact

# JSON output (pipe to jq)
python flow_attr_search.py --attribute myAttr --instance-id <UUID> --all --json | jq '.flows[] | select(.hit_count > 0)'
```

## Hit kinds

| Kind | Description |
|---|---|
| `SET` | Attribute key is assigned a value in an `UpdateContactAttributes` block |
| `CHECK` | Attribute value is the subject of a `Compare` block branch |
| `REF` | `$.Attributes.<name>` appears anywhere else in block parameters (Lambda inputs, prompt text, etc.) |

## APIs used

`ListContactFlows`, `DescribeContactFlow`

## Required IAM

- `connect:ListContactFlows`
- `connect:DescribeContactFlow`

## Key behaviors

- `--attribute` match is **case-insensitive by default**; add `--exact` for exact case
- Attribute name is matched as a whole token — searching `foo` will not match `fooBar`
- `--all` bulk mode shows a summary table; add `--detail` for per-block breakdown on flows with hits
- `--name` is a case-insensitive substring match; exits if multiple flows match
- Accepts both the `export_flow.py` envelope format (`{"metadata":..., "content":...}`) and raw flow JSON
- `--json` output includes `hit_count`, `set_count`, `check_count`, `ref_count`, and full `hits` array per flow
