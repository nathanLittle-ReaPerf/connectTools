# describe_resource.py

Look up any Amazon Connect resource by ARN. Parses a full or partial Connect ARN to determine the resource type, calls the appropriate Describe API, and displays the resource's key properties.

## Usage

```bash
# Full ARN — no other args needed (instance ID and region are embedded)
python describe_resource.py arn:aws:connect:us-east-1:123456789012:instance/UUID/queue/UUID

# Partial path fragment
python describe_resource.py instance/UUID/routing-profile/UUID --region us-east-1

# Bare resource ID — type and instance required
python describe_resource.py <resource-uuid> --type queue --instance-id UUID --region us-east-1

# Phone number ARN (no instance ID needed)
python describe_resource.py arn:aws:connect:us-east-1:123456789012:phone-number/UUID

# type/id shorthand
python describe_resource.py queue/UUID --instance-id UUID --region us-east-1

# JSON output
python describe_resource.py <arn> --json | jq '.Name'
```

| Flag | Description |
|---|---|
| `ARN` | Full Connect ARN, partial fragment, `type/id` pair, or bare UUID (positional) |
| `--instance-id UUID` | Instance UUID — required when not embedded in the ARN |
| `--region REGION` | AWS region — extracted from full ARN when present |
| `--profile NAME` | Named AWS profile for local use |
| `--type TYPE` | Resource type — required for bare IDs (see table below) |
| `--json` | Print full Describe response as JSON |

## Supported Resource Types

| `--type` value | Describe API | Key fields shown |
|---|---|---|
| `queue` | `DescribeQueue` | Name, Description, QueueType, Status, HoursOfOperationId, MaxContacts |
| `routing-profile` | `DescribeRoutingProfile` | Name, Description, DefaultOutboundQueueId, MediaConcurrencies |
| `contact-flow` | `DescribeContactFlow` | Name, Type, State, Status, Description |
| `contact-flow-module` | `DescribeContactFlowModule` | Name, Status, Description |
| `user` | `DescribeUser` | Username, IdentityInfo (full name + email), RoutingProfileId, SecurityProfileIds |
| `hours-of-operation` | `DescribeHoursOfOperation` | Name, Description, TimeZone, Config (day schedules) |
| `phone-number` | `DescribePhoneNumber` | PhoneNumber, Type, CountryCode, Status, TargetArn |
| `security-profile` | `DescribeSecurityProfile` | SecurityProfileName, Description, OrganizationResourceId |
| `quick-connect` | `DescribeQuickConnect` | Name, Description, QuickConnectType, QuickConnectConfig |
| `prompt` | `DescribePrompt` | Name, PromptARN |
| `agent-status` | `DescribeAgentStatus` | Name, Description, Type, State, DisplayOrder |

## ARN Format Reference

```
arn:aws:connect:<region>:<account>:instance/<instance-id>/<type>/<resource-id>
arn:aws:connect:<region>:<account>:phone-number/<phone-number-id>
```

The tool accepts input in any of these forms:
- Full ARN — everything is extracted automatically
- `instance/<instance-id>/<type>/<resource-id>` — needs `--region`
- `<type>/<resource-id>` — needs `--instance-id` and `--region`
- Bare UUID — needs `--type`, `--instance-id`, and `--region`

## APIs Used

One of the following, depending on the resource type:

- `DescribeQueue`
- `DescribeRoutingProfile`
- `DescribeContactFlow`
- `DescribeContactFlowModule`
- `DescribeUser`
- `DescribeHoursOfOperation`
- `DescribePhoneNumber`
- `DescribeSecurityProfile`
- `DescribeQuickConnect`
- `DescribePrompt`
- `DescribeAgentStatus`

## Required IAM

Only the permission for the resource type being looked up is needed:

- `connect:DescribeQueue`
- `connect:DescribeRoutingProfile`
- `connect:DescribeContactFlow`
- `connect:DescribeContactFlowModule`
- `connect:DescribeUser`
- `connect:DescribeHoursOfOperation`
- `connect:DescribePhoneNumber`
- `connect:DescribeSecurityProfile`
- `connect:DescribeQuickConnect`
- `connect:DescribePrompt`
- `connect:DescribeAgentStatus`
