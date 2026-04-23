# flow_promote.py

Promote Amazon Connect contact flows from a Dev instance to Prod. Exports each flow from Dev, remaps all embedded ARNs (queues, prompts, sub-flows, Lambdas, hours of operation, quick connects) to their Prod equivalents by name-matching, then imports the updated flows into Prod.

Sub-flow dependencies are detected automatically and resolved interactively — you choose whether to deploy or skip each one. At the end a summary lists every deployed and skipped flow.

## Dependencies

No pip install required beyond boto3. Requires snapshots for both instances (see [Prerequisites](#prerequisites)).

## Usage

```bash
python flow_promote.py --dev-instance-id <UUID> --prod-instance-id <UUID> \
    --name "Main IVR" [options]
```

| Flag | Description |
|---|---|
| `--dev-instance-id` | Dev Connect instance UUID (required) |
| `--prod-instance-id` | Prod Connect instance UUID (required) |
| `--name NAME` | Flow name to promote — exact, case-insensitive. Repeatable. |
| `--dev-region` | Dev AWS region (default: `us-east-1`) |
| `--prod-region` | Prod AWS region (default: same as `--dev-region`) |
| `--dev-profile` | AWS profile for Dev credentials |
| `--prod-profile` | AWS profile for Prod credentials |
| `--publish` | Publish flows after importing (default: leave as Draft) |
| `--backup-dir` | Directory for Prod backups before overwrite (default: `./flow_backups`) |
| `--no-backup` | Skip backing up Prod flows before overwriting |
| `--dry-run` | Show what would happen without making any changes |
| `--skip-unresolved` | Deploy even when some ARNs cannot be remapped (flagged in output) |
| `--refresh-snapshots` | Fetch fresh snapshots for both instances before starting |

## Prerequisites

Snapshots for both instances must exist before running:

```bash
python instance_snapshot.py --instance-id <dev-id>  --region us-east-1
python instance_snapshot.py --instance-id <prod-id> --region us-east-1
```

Or pass `--refresh-snapshots` to fetch them automatically at startup.

## Examples

```bash
# Dry run — see exactly what would be remapped and deployed
python flow_promote.py --dev-instance-id <dev> --prod-instance-id <prod> \
    --name "Main IVR" --dry-run

# Promote one flow (leaves it in Draft state for review)
python flow_promote.py --dev-instance-id <dev> --prod-instance-id <prod> \
    --name "Main IVR"

# Promote and publish immediately
python flow_promote.py --dev-instance-id <dev> --prod-instance-id <prod> \
    --name "Main IVR" --publish

# Promote multiple flows at once
python flow_promote.py --dev-instance-id <dev> --prod-instance-id <prod> \
    --name "Main IVR" --name "Auth Sub-Flow" --name "Queue Transfer" --publish

# Different AWS accounts for Dev and Prod
python flow_promote.py --dev-instance-id <dev> --prod-instance-id <prod> \
    --name "Main IVR" --dev-profile dev-admin --prod-profile prod-admin \
    --refresh-snapshots --publish

# Deploy even when some ARNs can't be remapped (with warnings)
python flow_promote.py --dev-instance-id <dev> --prod-instance-id <prod> \
    --name "Main IVR" --skip-unresolved --publish
```

## ARN Remapping

Every ARN embedded in the flow content is detected and remapped automatically:

| Resource | Strategy |
|---|---|
| Queues, prompts, hours of operation, quick connects | Name-matched from snapshots → Prod ARN substituted |
| Contact flows (sub-flows, transfers) | Name-matched from snapshots → Prod ARN substituted |
| Lambda functions | Account ID and region swapped; function name preserved |
| Lex / LexV2 bots | Flagged as unresolvable — require manual mapping |
| Bare instance ARN | Instance ID swapped |

Resources that cannot be matched (name not found in Prod snapshot, or Lex bots) appear as `⚠ Unresolved` warnings. By default the flow is skipped if any ARNs are unresolved; use `--skip-unresolved` to deploy anyway with the broken references in place.

## Sub-Flow Dependency Handling

When a flow references other flows (sub-flows, queue transfer flows, whisper flows), the tool detects them before deploying. For each dependency not already in the promotion list:

- **Exists in Prod:** asks whether to promote the Dev version too. Answering "no" is safe — the remap succeeds using the existing Prod ARN.
- **Missing from Prod:** warns that the flow will break at runtime. Asks whether to deploy it. Answering "no" skips that dependency; the parent flow is still deployed but flagged in the summary.

Flows deploy in **dependency order** (sub-flows first, entry-point flows last). As each flow is created in Prod its new ARN is registered so subsequent flows in the same session can reference it.

## Backups

Before overwriting any existing Prod flow, the current version is saved to `./flow_backups/<FlowName>_YYYYMMDD_HHMMSS.json` (the same envelope format as `export_flow.py`). Use `--no-backup` to skip this. Backups are skipped automatically in `--dry-run` mode.

## Draft vs. Published

By default, imported flows are left in **Draft** state — they are present in Prod but the live routing is unchanged until you publish them. This allows review before cutover. Pass `--publish` to publish immediately after import, or publish manually in the Connect console.

## Summary Output

At the end of a run the tool prints a full summary:

```
══════════════════════════════════════════════════════
  SUMMARY
══════════════════════════════════════════════════════

DEPLOYED — review recommended:
  ✓  Main IVR                                    UPDATED [DRAFT]
       ⚠  Dep "Queue Transfer Flow" was skipped — may fail at runtime
  ✓  Auth Sub-Flow                               CREATED [DRAFT]

SKIPPED / FAILED — manual action needed:
  ✗  Queue Transfer Flow                         SKIPPED (referenced by: Main IVR)
  ✗  Bad Lambda Flow                             SKIPPED (unresolved ARNs)

Flows are in DRAFT state. Publish in the Connect console or re-run with --publish.
```

## Required IAM Permissions

**Dev (source):**
```
connect:ListContactFlows
connect:DescribeContactFlow
```

**Prod (destination):**
```
connect:ListContactFlows
connect:DescribeContactFlow
connect:CreateContactFlow
connect:UpdateContactFlowContent
connect:PublishContactFlow   ← only with --publish
sts:GetCallerIdentity
```

## APIs Used

| API | Purpose |
|---|---|
| `ListContactFlows` | Find flows by name in Dev and Prod |
| `DescribeContactFlow` | Export flow content from Dev; backup Prod before overwriting |
| `CreateContactFlow` | Create a new flow in Prod |
| `UpdateContactFlowContent` | Overwrite an existing Prod flow's content |
| `PublishContactFlow` | Publish the flow (only with `--publish`) |
| `sts:GetCallerIdentity` | Determine Prod account ID for Lambda ARN remapping |

## Changelog

| Version | Change |
|---|---|
| Initial | ARN remapping for queues, flows, prompts, HOO, quick connects, Lambda; interactive dep resolution; topo-sort deploy order; dry run; backup; summary |
