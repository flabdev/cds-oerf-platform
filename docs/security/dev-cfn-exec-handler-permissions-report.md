# Dev CFN execution policy: Resource `*` allows and handler N/A rules

Credential-free checks live in `infra/cfn_handler_permission_tests.py` and
`tests/unit/test_cfn_handler_permissions.py`. Policy sizes use
`infra.dev_boundary.nonwhitespace_size` (IAM 6,144 non-whitespace limit).

## Policy sizes (placeholder account `000000000000`, `us-east-1`)

| Policy | Non-whitespace chars | Slack |
| --- | ---:| ---:|
| OerfDevCfnExecPolicy | 5840 | 304 |
| OerfDevCfnExecDenies | 2628 | 3516 |
| OerfDevWorkloadBoundary | 6036 | **108** (≥ 34 headroom requested) |

## OerfDevCfnExecPolicy: every `Allow` on `Resource "*" `

Programmatic list: `infra.dev_boundary.exec_policy_resource_star_rows()` returns
`(Sid, action pattern, reason)`.

| Sid | Action | Reason |
| --- | --- | --- |
| U | `apigateway:*` | REST control plane has no stage-scoped ARN until create returns an id |
| U | `apigatewayv2:*` | HTTP/WebSocket control plane tags are separate; create has no id yet |
| U | `cloudfront:*` | Tag-unfriendly reads and OAC lifecycle; fenced elsewhere where needed |
| U | `cloudwatch:DescribeAlarms` | Describe has no resource-scoped denial matching dev names |
| U | `cognito-idp:*` | User pool CRUD is not expressible as a single dev ARN pattern in NotResource |
| U | `ecr:GetAuthorizationToken` | ECR auth has no resource ARN |
| U | `lambda:*EventSourceMapping*` | Event source mapping ARNs are not folded into dev name patterns |
| U | `logs:*LogDeliver*` | Cross-service log delivery control plane uses unscopeable ARNs |
| U | `logs:*ResourcePolic*` | Account-wide logs resource policies are not dev-name scoped |
| U | `logs:DescribeLogGroups` | Describe matches names outside strict dev ARN forms |
| U | `iam:ListRoles` | ListRoles is account-level; used by IAM::Role handler list operation |
| U | `bedrock:CreateGuardrail` | Create authorizes on guardrail-profile; new guardrail id unknown |
| U | `bedrock:CreateGuardrailVersion` | Version create follows same unscoped pattern as create |
| U | `bedrock:DeleteGuardrail` | Service lists guardrail APIs as authorizing on resource * |
| U | `bedrock:GetGuardrail` | Same as delete for read path on handler |
| U | `bedrock:ListGuardrails` | List is account-wide in the service model |
| U | `bedrock:ListTagsForResource` | Handler list-tags path per resource |
| U | `bedrock:TagResource` | Tagging dev guardrail after create |
| U | `bedrock:UntagResource` | Untag on dev guardrail |
| U | `bedrock:UpdateGuardrail` | Update on dev guardrail |
| EC2D | `ec2:Describe*` | Lambda and AgentCore handlers describe VPCs/subnets/security groups without a single dev ARN |
| KFH | `kinesis:*` | DynamoDB table Kinesis streaming destination uses a dev-named stream ARN |
| KFH | `firehose:TagDeliveryStream` | CloudWatch Logs subscription delivery may tag a dev-named Firehose delivery stream |

`STAR_ACTION_REASONS` (NOD `NotAction`) also lists `kinesis:*` and `firehose:TagDeliveryStream`
so scoped `KFH` allows are not contradicted by the name deny.

## Template-based “not applicable” (handler checker)

Rules: `template_property_rules()` plus special cases for vended log delivery and
optional Bedrock `KmsKeyArn`. Each skip row is `(CFN type, action, reason)` with the
reason naming the property checked.

Examples:

- `AWS::IAM::Policy` + `iam:*UserPolicy*` / `iam:*GroupPolicy*` → Users/Groups absent on every inline policy.
- `AWS::IAM::Policy` + `iam:GetUserPolicy` / `iam:GetGroupPolicy` → Users / Groups respectively.
- `AWS::IAM::ManagedPolicy` + attach/detach user or group → `Users` / `Groups` property absent.
- `AWS::Lambda::Function` + `ec2:*`, `elasticfilesystem:*`, `s3files:*` → `VpcConfig` / `FileSystemConfigs` absent.
- `AWS::DynamoDB::Table` + `kinesis:*` → `KinesisStreamSpecification` absent.
- `AWS::S3::Bucket` + `s3tables:*` → `MetadataTableConfiguration` absent.
- `AWS::BedrockAgentCore::Runtime` + `vpc-lattice:*`, `ec2:CreateNetworkInterface` → private VPC-style `NetworkConfiguration` absent (public network only).
- `logs:AllowVendedLogDeliveryForResource` / third-party `*AllowVendedLogDeliveryForResource` → `AWS::Logs::DeliverySource` `ResourceArn` service prefixes.
- `firehose:*` → `AWS::Logs::Delivery` absent (no delivery graph in dev synth).

If a dev template later sets a skipped property, `check_handler_permissions` raises and fails the test.

## Denies: NU (exec + boundary)

Statement `NU` denies IAM user/group inline policy mutations and group membership changes
(listed explicitly so `GetUserPolicy` / `GetGroupPolicy` stay allowed for role-only flows).

## Tests and script

- `pytest tests/unit/test_cfn_handler_permissions.py tests/unit/test_dev_boundary.py`
- `scripts/check_dev_boundary.py` `FENCE_CHECKS`: `iam:PutUserPolicy`, `iam:AttachGroupPolicy`,
  `ec2:CreateNetworkInterface` denied for exec and router roles (via `_quad`).
