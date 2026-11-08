
# Dev CFN execution policy: handler probes and N/A rules

Credential-free checks: `infra/cfn_handler_permission_tests.py`,
`infra/service_reference_probes.py`, `tests/unit/test_cfn_handler_permissions.py`.
Fixtures: `infra/bootstrap/iam-fixtures/cfn-handler-permissions.json` and
`service-reference.json` (refresh: `./scripts/refresh_iam_fixtures.sh`).
Live/repo parity: `scripts/check_dev_boundary.py --handler-permissions` evaluates
**`OerfDevCfnExecPolicy` + `OerfDevCfnExecPolicy2` + `OerfDevCfnExecDenies`**
with `OerfDevWorkloadBoundary`.

## Policy split (`OerfCiStack-dev`)

| Managed policy | Contents |
| --- | --- |
| `OerfDevCfnExecPolicy` | `DN`, `DS3`, `AR`, `MT`, `BS`, `KFH`, role create/mutate/pass (`CRB`, `MRB`, `DRR`, `PR`) |
| `OerfDevCfnExecPolicy2` | `U`, `EC2D`, `K1`, `KV`, `K2`, `SLR`, tag-deny statements (`TD`, `RT`, …) |
| `OerfDevCfnExecDenies` | exec copy of boundary workload denies + guardrail ARN deny |

Bootstrap `CloudFormationExecutionPolicies`: all **three** identity ARNs (two allows + denies).

### Boundary compression (`chore/compress-dev-boundary`)

| | Non-whitespace chars | Slack |
| --- | ---:| ---:|
| Before (baseline fixture) | 6141 | 3 |
| After | 5704 | 440 |
| Saved | **437** | |

Equivalence: `tests/unit/test_boundary_equivalence.py` runs **3052** cases over the
**351** IAM actions in the **trimmed** `service-reference.json` fixture (not the full
AWS catalog) × dev/main/`*` probes × four tag contexts. **264** cases are
**expected-stricter** (see below); the test fails on any other mismatch or any case
where the compressed boundary **allows** more than the baseline.

`test_notaction_exemption_patterns_do_not_widen_baseline` applies two checks: (1)
**lexical** — every **NOD** / **NMT** `NotAction` entry must appear **verbatim** in
the baseline fixture’s `NotAction` lists, or be an **exact** action name with no
`*` (new wildcard patterns fail even when trimmed-fixture expansion looks safe); (2)
**fixture expansion** — each pattern’s matches in `service-reference.json` must stay
within the baseline exempt union.

#### Not used: `bedrock:*Guardrail*`

In `service-reference.json`, `bedrock:*Guardrail*` would expand to:
`CreateGuardrail`, `CreateGuardrailVersion`, `DeleteGuardrail`, `GetGuardrail`,
`ListGuardrails`, `UpdateGuardrail` (not `ApplyGuardrail` or `ListTagsForResource` in
the trimmed fixture; `ApplyGuardrail` is kept as an **explicit** NOD exempt because
it is part of the pre-compression set and account-level guardrail APIs on `*` must
not gain new exemptions). Broader patterns such as enforced-guardrail-configuration
are intentionally **not** folded into a guardrail name wildcard.

#### NOD / NMT exemption wildcards still present (fixture expansions)

| Pattern | Service-reference expansion |
| --- | --- |
| `bedrock:*agResource` | `TagResource`, `UntagResource` |
| `kms:*` | `CreateGrant`, `Decrypt`, `DescribeKey`, `Encrypt`, `GenerateDataKey`, `GenerateDataKeyWithoutPlaintext`, `ListAliases`, `ReEncryptFrom`, `ReEncryptTo`, `RetireGrant`, `RevokeGrant` (same union as baseline; explicit `kms:Decrypt` / `DescribeKey` / `Encrypt` removed from `STAR_ACTION_REASONS` only) |
| `lambda:*EventSourceMapping*` | `CreateEventSourceMapping`, `DeleteEventSourceMapping`, `GetEventSourceMapping`, `ListEventSourceMappings`, `UpdateEventSourceMapping` |
| `logs:*LogDeliver*` | `CreateLogDelivery` |
| `logs:*ResourcePolic*` | `DeleteResourcePolicy`, `DescribeResourcePolicies`, `PutResourcePolicy` |
| `logs:Describe*` | seven `logs:Describe*` actions in the fixture |
| `ec2:Describe*` | `DescribeNetworkInterfaces`, `DescribeSecurityGroups`, `DescribeSubnets`, `DescribeVpcs` |
| `apigateway:*`, `cloudfront:*`, `cognito-idp:*`, `kinesis:*`, `xray:*` | full prefix lists in the fixture (unchanged vs baseline union) |

Explicit guardrail NOD exempts (not wildcards): `ApplyGuardrail`,
`CreateGuardrailVersion`, `DeleteGuardrail`, `GetGuardrail`, `ListGuardrails`,
`UpdateGuardrail`, plus `bedrock:CreateGuardrail` in `STAR_ACTION_REASONS` and
`bedrock:ListTagsForResource` in `STAR_ACTION_REASONS`.

#### Expected-stricter (264 cases)

Removed `NotResource`
`arn:aws:logs:…:log-group:/aws/bedrock-agentcore/runtimes/*`. Main runtime log ARNs
without `_dev_` (for example `…/runtimes/oerf_agent`) were previously exempt from
**NOD** via that entry; they now fall under name deny unless matched by
`logs:*_dev_*`. Dev AgentCore log paths (`oerf_dev_agent`) are unchanged.

Other compressions: `OerfGitHubDeployRole*`; `account:*` / `organizations:*` in **NU**
(removed **NO**); cosmetic **Sid** drops; **NPE** `iam:*PolicyVersion`; **NIOD**
`iam:PutRole*`; **NU** `iam:Delete*PermissionsBoundary`.

## Policy sizes (`000000000000`, `us-east-1`)

| Policy | Non-whitespace chars | Slack |
| --- | ---:| ---:|
| OerfDevCfnExecPolicy | 2999 | 3145 |
| OerfDevCfnExecPolicy2 | 3028 | 3116 |
| OerfDevCfnExecDenies | 2681 | 3463 |
| OerfDevWorkloadBoundary | 5704 | 440 |

### KMS (dev)

Dev templates use AWS-managed keys and SSE-S3 for the asset bucket only. The account
has one non-dev customer CMK (`d3fdd4e1-…`, no alias). Statement **NU** denies CMK
administration (`kms:Schedule*`, `kms:PutKeyPolicy`, `kms:Disable*`, `kms:Delete*`,
`kms:Update*`, `kms:CreateKey`, `kms:CreateAlias`) on `Resource` `*`
in both `OerfDevWorkloadBoundary` and `OerfDevCfnExecDenies`. No handler fixture
uses those actions; customer-key Bedrock paths stay N/A.

`alias/cdk-oerfdev-assets-key` does **not** exist in the account today (dev asset
bucket uses S3-managed encryption), so statement **K1** in `OerfDevCfnExecPolicy2`
is currently unused.

## Handler check

**466** deploy simulations, **53** skips (unchanged after boundary compress;
includes EventBus `PutPermission` /
`RemovePermission` when `Policy` is unset), **47** list-informational, **0** gaps.

Handler `--handler-permissions` should pass the same `--inbound-topic-arn` as the
boundary check when dev subscribes to main's inbound topic (sets
`whatsapp:sharedInboundTopicArn` for synth).

### Residuals (live IAM simulator)

`iam:SimulatePrincipalPolicy` on the dev CFN exec role reports **implicitDeny**
with empty matched statements for some SNS handler actions probed against a
**topic** ARN, while **sns:Subscribe** on the same ARN often matches statement
**MT**. Repo-exec (`intersection_allows` over Policy + Policy2 + Denies +
boundary) still **PASS**; deployed policies grant these via **MT** (shared main
inbound topic) or **DN** (dev-only topic name).

Live check reports these **exact** `(CloudFormation type, action)` pairs as
**UNVERIFIABLE** (not FAIL); any other implicitDeny when allow is expected
stays **FAIL**.

| Type | Action |
| --- | --- |
| `AWS::SNS::Subscription` | `sns:GetSubscriptionAttributes` |
| `AWS::SNS::Subscription` | `sns:SetSubscriptionAttributes` |
| `AWS::SNS::Subscription` | `sns:Unsubscribe` |
| `AWS::SNS::Topic` | `sns:Unsubscribe` |

**Verification plan:** On the first dev deploy that changes or removes the
subscription from the dev stack to main's inbound topic, watch CloudTrail
(`lookup-events` filtered to `sns.amazonaws.com` and the CFN exec role). If
**AccessDenied** appears for any of the actions above, add the needed statement
or row in `docs/security/iam-exceptions.md` and extend the exec policies
accordingly.

### Fixture size (consolidation)

| Artifact | Before (dirs) | After (single file) |
| --- | ---: | ---:|
| Handler permissions | 40,570 B (50 files) | 37,976 B |
| Service reference | 2,819,567 B (21 files) | 148,910 B |

```bash
JSII_RUNTIME_PACKAGE_CACHE_ROOT="$(pwd)/.jsii-cache" \
  .venv311/bin/python -m pytest -q \
  tests/unit/test_cfn_handler_permissions.py \
  tests/unit/test_dev_boundary.py::test_policy_documents_fit_the_managed_policy_quota \
  tests/unit/test_boundary_equivalence.py
```
