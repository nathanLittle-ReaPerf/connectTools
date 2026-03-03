# CloudShell Setup — Contacts Handled Script

## Prerequisites
- AWS Console access with permissions for:
  - `connect:ListQueues`
  - `connect:GetMetricDataV2`
  - `sts:GetCallerIdentity`

---

## Step 1 — Open CloudShell
In the AWS Console, click the **CloudShell** icon in the top navigation bar (terminal icon, top right).

Wait for the shell to initialize.

---

## Step 2 — Install Dependency (one time only)
```bash
pip install python-dateutil --user
```
This persists across CloudShell sessions — you only need to do this once.

---

## Step 3 — Upload the Script
1. In the CloudShell window, click **Actions** (top right of the shell panel)
2. Select **Upload file**
3. Upload `contacts_handled.py`

Verify it uploaded:
```bash
ls ~/contacts_handled.py
```

---

## Step 4 — Run the Script
```bash
python contacts_handled.py --instance-id <your-instance-id> --region <your-region>
```

**Example:**
```bash
python contacts_handled.py --instance-id 12345678-aaaa-bbbb-cccc-1234567890ab --region us-east-1
```

> No `--profile` needed — CloudShell uses your console session credentials automatically.

---

## Expected Output
```
2026-02-01 to 2026-03-01 (UTC): 12,454 Contacts Handled
```

---

## Notes
- The script always reports the **previous calendar month**.
- Your instance ID can be found in the Amazon Connect console under **Instance settings**.
- If you get a permissions error, verify your IAM role/user has the required Connect permissions listed above.
