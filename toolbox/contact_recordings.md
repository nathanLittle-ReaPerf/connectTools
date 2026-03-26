# contact_recordings.py — Contact Recordings & Transcripts

Locate the S3 paths and generate presigned download URLs for a contact's recordings and transcripts — original and redacted — for both voice and chat.

```bash
# Human-readable output
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --region us-east-1

# Extend presigned URL expiry to 2 hours
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --url-expires 7200

# Raw JSON (pipe to jq)
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --json | jq '.artifacts'

# With a named AWS profile (local dev)
python contact_recordings.py --instance-id <UUID> --contact-id <UUID> --profile my-admin
```

**APIs used:** `DescribeContact`, `ListInstanceStorageConfigs`, `s3:ListObjectsV2`, `s3:GeneratePresignedUrl`

**Required IAM:**
- `connect:DescribeContact`
- `connect:ListInstanceStorageConfigs`
- `s3:ListBucket` on the recordings/transcripts bucket(s)
- `s3:GetObject` on the recordings/transcripts bucket(s)

## What it finds

The script reads your instance's storage configuration at runtime — no hardcoded bucket names.

### VOICE contacts

| Type | S3 prefix (under BucketPrefix) |
|---|---|
| Recording (original) | `CallRecordings/YYYY/MM/DD/` |
| Recording (redacted) | `CallRecordings/YYYY/MM/DD/` — filename contains `_redacted` |
| Contact Lens analysis (original) | `Analysis/Voice/YYYY/MM/DD/` |
| Contact Lens analysis (redacted) | `Analysis/Voice/Redacted/YYYY/MM/DD/` |

### CHAT contacts

| Type | Storage config | S3 prefix |
|---|---|---|
| Transcript (original) | `CHAT_TRANSCRIPTS` | `YYYY/MM/DD/` |
| Transcript (redacted) | `CHAT_TRANSCRIPTS` | `Redacted/YYYY/MM/DD/` |
| Contact Lens analysis (original) | `CALL_RECORDINGS` | `Analysis/Chat/YYYY/MM/DD/` |
| Contact Lens analysis (redacted) | `CALL_RECORDINGS` | `Analysis/Chat/Redacted/YYYY/MM/DD/` |

## Key behaviors

- Reads `ListInstanceStorageConfigs` for `CALL_RECORDINGS` and `CHAT_TRANSCRIPTS` — adapts to your instance's bucket names and prefixes automatically
- Searches S3 under the contact's date prefix and filters results by contact ID in the key name
- Classifies files as **original** or **redacted** by checking for `_redacted` in the filename or `/Redacted/` in the path
- Presigned URLs default to **1-hour** expiry; override with `--url-expires <seconds>`
- If Contact Lens was not enabled or has not produced output, those sections show "(none found)" rather than erroring
- `--json` output groups all results under `artifacts.recordings`, `artifacts.analysis`, and `artifacts.transcripts`
