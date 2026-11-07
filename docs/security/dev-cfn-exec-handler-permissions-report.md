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

## Policy sizes (`000000000000`, `us-east-1`)

| Policy | Non-whitespace chars | Slack |
| --- | ---:| ---:|
| OerfDevCfnExecPolicy | 2999 | 3145 |
| OerfDevCfnExecPolicy2 | 3028 | 3116 |
| OerfDevCfnExecDenies | 2681 | 3463 |
| OerfDevWorkloadBoundary | 6141 | 3 |

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

**466** deploy simulations, **53** skips (includes EventBus `PutPermission` /
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
| --- | ---: | ---: |
| Handler permissions | 40,570 B (50 files) | 37,976 B |
| Service reference | 2,819,567 B (21 files) | 148,910 B |

```bash
JSII_RUNTIME_PACKAGE_CACHE_ROOT="$(pwd)/.jsii-cache" \
  .venv311/bin/python -m pytest -q \
  tests/unit/test_cfn_handler_permissions.py \
  tests/unit/test_dev_boundary.py::test_policy_documents_fit_the_managed_policy_quota
```
