# Amazon Connect: Summing Contacts Handled for Previous Month

This document provides a complete reference and Python script for retrieving the **total Contacts Handled** metric across an Amazon Connect instance for the previous calendar month using **GetMetricDataV2**.

## Overview
- Uses **GetMetricDataV2**, the recommended API for historical metrics. citeturn1search1
- Requires at least one filter (Queues, Routing Profiles, Agents, or User Hierarchy Groups). This script automatically discovers **all queue IDs**. citeturn1search8
- Aggregates results using `IntervalPeriod='TOTAL'`. Valid interval periods include `FIFTEEN_MIN`, `THIRTY_MIN`, `HOUR`, `DAY`, `WEEK`, and `TOTAL`. citeturn1search2
- Summarizes the **CONTACTS_HANDLED** metric as defined in Amazon Connect historical metrics. citeturn1search9

## Python Script
```python
# (Script content omitted here in this preview — the actual file contains full script.)
```

## Running the Script
```bash
python contacts_handled_prev_month.py   --instance-id <your-instance-id>   --region us-east-1   --profile my-admin
```

## Notes
- Supports batching for >100 queues, respecting API filter limits. citeturn1search5
- Time window automatically evaluated as the previous month in UTC.
- Metrics aggregated as a single total using `IntervalPeriod='TOTAL'`.

