# Dev CDK bootstrap

Main stays on the default bootstrap: qualifier `hnb659fds`, toolkit stack `CDKToolkit`. Dev uses a second bootstrap so its deploy role cannot change main's stacks. The file-publishing, image-publishing, and lookup roles are the stock ones. Their names follow the qualifier, so the asset bucket is `cdk-oerfdev-assets-<account>-<region>` and the repository is `cdk-oerfdev-container-assets-<account>-<region>`.

`dev-bootstrap.yaml` is the template from `cdk bootstrap --show-template` for CLI 2.1145.0. `CdkBootstrapVersion` is **32**. The stock `BootstrapVariant` was `AWS CDK: Default Resources`. This copy sets it to `OERF dev: CloudFormation deploy role limited to *-dev stacks` so the CLI will not overwrite the stack with the stock template.

What changed from that stock template:

- The deploy role's CloudFormation actions allow `stack/*-dev/*` and `changeSet/*/*-dev*`. Change-set calls are authorized against the change set ARN, not only the stack ARN. `CDKToolkit-dev` is not in that allow list.
- `DescribeStacks`, `ListStacks`, `ValidateTemplate`, and `GetTemplateSummary` stay on `Resource: *`. They have no stack to name, or the CLI calls them without one. A deploy reads the toolkit stack only through that `DescribeStacks` allow and through `ssm:GetParameter` on `/cdk-bootstrap/oerfdev/version`.
- An explicit deny blocks every `cloudformation:*` action on `stack/OerfCiStack-dev/*` and its change sets. A second deny blocks CloudFormation writes on `stack/CDKToolkit-dev/*` and its change sets. `*-dev` would otherwise include the toolkit stack. Ananda changes `CDKToolkit-dev` and `OerfCiStack-dev` only from his laptop.
- `AWSCloudFormationReadOnlyAccess` is not attached. It would have allowed reads of every stack, including main.
- `iam:PassRole` is still only `${CloudFormationExecutionRole}`. With qualifier `oerfdev` that is `cdk-oerfdev-cfn-exec-role-<account>-<region>`.
- The execution role is `cdk-oerfdev-cfn-exec-role-<account>-<region>`. After `OerfCiStack-dev` has created `OerfDevCfnExecPolicy`, `OerfDevCfnExecPolicy2`, `OerfDevCfnExecDenies`, and `OerfDevWorkloadBoundary`, re-bootstrap so this role uses all three identity policies and that permissions boundary (section 2b). The template parameters are `CloudFormationExecutionPolicies` (a comma-separated list of the two allow policies plus the deny policy) and `InputPermissionsBoundary`. A first bootstrap, before those policies exist, still passes `AdministratorAccess` and does not set a boundary. Do not leave that in place.

Bootstrap it only as stack `CDKToolkit-dev` with `--qualifier oerfdev` and no `--trust`. Do not apply this file to `CDKToolkit`.

When the CDK CLI is upgraded, run `cdk bootstrap --show-template`, diff it against this file, and re-apply only the CloudFormation limits above. Keep `BootstrapVariant` custom so the new stock template does not replace this one. Bump the recorded version if `CdkBootstrapVersion` changes.

## CloudFormation handler permissions (repo fixtures)

The folder `infra/bootstrap/cfn-handler-permissions/` stores one JSON file per CloudFormation resource type the four dev application stacks use. Each file has a top-level `"type"` and a `"handlers"` object whose keys are `create`, `read`, `update`, `delete`, and `list`; each value is the list of IAM actions the handler performs (from the registered type schema).

**Refresh when:** you add a new `AWS::…` type to a dev app template, or AWS publishes new handler actions for a type you already use.

**How to refresh:** for each type name `T` (for example `AWS::Bedrock::Guardrail`), fetch the resource type schema and copy the handler permission lists into `infra/bootstrap/cfn-handler-permissions/<T with :: replaced by __>.json`. A typical loop uses the CloudFormation API, for example:

```bash
for TYPE in AWS::Service::ResourceType AWS::Other::Type; do
  SAFE=${TYPE//::/__}
  aws cloudformation describe-type \
    --type RESOURCE \
    --type-name "$TYPE" \
    --query 'Schema' \
    --output text | jq . > "infra/bootstrap/cfn-handler-permissions/${SAFE}.json"
done
```

Adjust the `TYPE` list to match the union of resource types in the dev synth. Merge or edit the saved JSON so it matches the shape above (`type` + `handlers` with IAM action strings) if the raw schema differs.

**What the check does not simulate**

- `AWS::CloudFormation::CustomResource` has no handler IAM policy; CloudFormation invokes the provider Lambda. The unit test and the live script list that type as skipped and rely on `AWS::Lambda::Function` for the Lambda’s own actions.
- For `AWS::Bedrock::Guardrail` / `AWS::Bedrock::GuardrailVersion`, handler `kms:*` calls apply only when the template sets `KmsKeyArn`. The dev stack does not set it (`BedrockGuardrail` in `infra/constructs/bedrock_guardrail.py`), so those actions are **not applicable** unless you opt in with `--handler-bedrock-guardrail-kms-key`. Do not widen policies with a bare `kms:*` allow to satisfy the schema.

**CI:** `.github/workflows/ci.yml` runs `pytest -q -m "not aws"`, which includes `tests/unit/test_cfn_handler_permissions.py`—the credential-free check that every handler action is allowed by `OerfDevCfnExecPolicy`, `OerfDevCfnExecPolicy2`, `OerfDevCfnExecDenies`, and `OerfDevWorkloadBoundary` for the dev probes.

**Live check (execution role):** after the managed policies exist in the account, simulate the same probes with no tag context against `cdk-oerfdev-cfn-exec-role-<account>-<region>`:

```bash
.venv311/bin/python -m scripts.check_dev_boundary \
  --profile Fission-OERF \
  --region us-east-1 \
  --account 906048429638 \
  --dev-guardrail-arn "arn:aws:bedrock:us-east-1:906048429638:guardrail/<id>" \
  --handler-permissions infra/bootstrap/cfn-handler-permissions
```

You can omit the path and use `--handler-permissions` alone (default directory). `--main-guardrail-arn` is optional in this mode; other router or channel flags are not required.

**Repo vs live:** each applicable handler action is checked twice: `repo-exec` evaluates `infra/dev_boundary.py` locally (no IAM API). `live-exec` calls `iam:SimulatePrincipalPolicy` on the deployed `cdk-oerfdev-cfn-exec-role` and needs valid credentials (`aws sso login` when using SSO). If credentials are expired, refresh the profile or add `--handler-repo-only` to run only the local check.

To include Bedrock guardrail customer-KMS handler simulations (only if you deploy with `KmsKeyArn` on the guardrail), add `--handler-bedrock-guardrail-kms-key`.

## Laptop deploy

Run these from the repo root. Use the project Python 3.11 venv so `cdk` can import `aws_cdk`:

```bash
export AWS_PROFILE=Fission-OERF
export AWS_DEFAULT_REGION=us-east-1
source .venv311/bin/activate
python3 -c "import aws_cdk; print('ok')"
```

Unset stale session variables if `aws sts get-caller-identity` fails with `InvalidClientTokenId`:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws sts get-caller-identity --profile Fission-OERF --region us-east-1
```

Every `aws` command below repeats `--profile Fission-OERF --region us-east-1` so it stays correct even when the exports are not set. `cdk` uses the exported profile and region.

Do not pass `whatsapp:wabaId` for dev. Dev does not create the WABA event destination. Dev receives the same inbound events by subscribing its queue to main's inbound topic.

Find that topic ARN with a read-only call. The logical id starts with `InboundTopic`. The physical id is the topic ARN. Do not print it into a ticket.

```bash
aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfComputeStack \
  --query "StackResourceSummaries[?ResourceType=='AWS::SNS::Topic' && starts_with(LogicalResourceId, 'InboundTopic')].PhysicalResourceId" \
  --output text
```

Set that value as the GitHub environment variable `SHARED_INBOUND_TOPIC_ARN` on the `dev` environment. The deploy workflow passes `-c whatsapp:sharedInboundTopicArn=...` when the variable is non-empty. A laptop deploy passes the same flag. Leave the flag off only when the ARN is not known yet; dev then keeps a queue subscription to its own topic, and nothing publishes there.

### 1. Bootstrap `CDKToolkit-dev`

```bash
cdk bootstrap aws://906048429638/us-east-1 \
  --profile Fission-OERF \
  --toolkit-stack-name CDKToolkit-dev \
  --qualifier oerfdev \
  --template infra/bootstrap/dev-bootstrap.yaml \
  --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess \
  -c stage=dev \
  -c account=906048429638
```

Use laptop credentials with permission to create the toolkit stack. Do not apply this template to main’s `CDKToolkit`.

### 2. Deploy `OerfCiStack-dev` (GitHub role)

`cdk deploy OerfCiStack-dev` fails on purpose: the oerfdev deploy role has an explicit deny on this stack. Synth, then create or update the stack with CloudFormation as your laptop principal (not via the bootstrap deploy role):

```bash
cdk synth --quiet \
  --profile Fission-OERF \
  -c stage=dev \
  -c account=906048429638 \
  -c ci:deployRoleStack=true \
  -c github:oidcProviderArn=arn:aws:iam::906048429638:oidc-provider/token.actions.githubusercontent.com \
  -c dev:guardrailArn=arn:aws:bedrock:us-east-1:906048429638:guardrail/GUARDRAIL_ID

aws cloudformation deploy \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfCiStack-dev \
  --template-file cdk.out/OerfCiStack-dev.template.json \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset
```

Replace the OIDC provider ARN if the account’s GitHub provider ARN differs. Replace `GUARDRAIL_ID` with the dev guardrail id. `dev:guardrailArn` is required with `ci:deployRoleStack=true` and must be `arn:aws:bedrock:<region>:<account>:guardrail/<id>` for this stack. Synth fails otherwise.

Guardrail calls are allowed only on that ARN, on the same ARN with a version suffix, and on `arn:aws:bedrock:*:<account>:guardrail-profile/*`. If the dev guardrail is replaced (new id), redeploy `OerfCiStack-dev` with the new `dev:guardrailArn` before the next dev deploy. Until that redeploy, guardrail calls are denied.

Read the id from the dev compute stack when it exists:

```bash
aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfComputeStack-dev \
  --query "StackResourceSummaries[?ResourceType=='AWS::Bedrock::Guardrail'].PhysicalResourceId" \
  --output text
```

The ARN is `arn:aws:bedrock:us-east-1:906048429638:guardrail/<that id>`. The guardrail deployed before the tag check was removed is `b7nun5huxbii`.

This stack also creates `OerfDevCfnExecPolicy`, `OerfDevCfnExecPolicy2`, `OerfDevCfnExecDenies`, and `OerfDevWorkloadBoundary`. The allows are split across two managed policies because dev name patterns and star-resource allows do not fit in one. Deploy the stack before the re-bootstrap below. The dev application stacks do not create these policies. They only name the boundary.

### 2b. Re-bootstrap and check the boundary

The policies from section 2 must exist first. This updates `CDKToolkit-dev` only. It does not change main’s `CDKToolkit`. The template already has `CloudFormationExecutionPolicies` and `InputPermissionsBoundary`; this command sets them. `--custom-permissions-boundary` is the policy name. The template attaches that boundary to the CloudFormation execution role only. The deploy, file-publishing, image-publishing, and lookup roles are unchanged and do not get it.

```bash
cdk bootstrap aws://906048429638/us-east-1 \
  --profile Fission-OERF \
  --toolkit-stack-name CDKToolkit-dev \
  --qualifier oerfdev \
  --template infra/bootstrap/dev-bootstrap.yaml \
  --cloudformation-execution-policies arn:aws:iam::906048429638:policy/OerfDevCfnExecPolicy,arn:aws:iam::906048429638:policy/OerfDevCfnExecPolicy2,arn:aws:iam::906048429638:policy/OerfDevCfnExecDenies \
  --custom-permissions-boundary OerfDevWorkloadBoundary \
  -c stage=dev \
  -c account=906048429638
```

After any later change to those policies, deploy `OerfCiStack-dev` again (section 2), then re-run this bootstrap, then the check. A failed dev application deploy rolls back that stack and does not change main.

### First rollout and tag changes

The first application deploy under the locked-down execution role needs a short transition. CloudFormation resolves each dev template’s `BootstrapVersion` parameter (`AWS::SSM::Parameter::Value<String>`, default `/cdk-bootstrap/oerfdev/version`) as **the execution role**, not the deploy role. `OerfDevCfnExecPolicy` allows `ssm:GetParameter` and `ssm:GetParameters` only on `arn:aws:ssm:<region>:<account>:parameter/cdk-bootstrap/oerfdev/*`. Redeploy `OerfCiStack-dev` after that policy change before relying on the steps below.

1. **Re-bootstrap once with `AdministratorAccess`** on the execution role (section 2b command, but pass `arn:aws:iam::aws:policy/AdministratorAccess` and omit `--custom-permissions-boundary`). Deploy the four dev app stacks with every context value set: `whatsapp:phoneNumberId`, `whatsapp:sharedInboundTopicArn`, and `alarms:email` where you use alarms. This is the transition deploy that adds `PermissionsBoundary` and `oerf:stage=dev` on taggable resources.

2. **Check the execution role boundary.** Re-bootstrap with the OERF policies may print `Removing existing permissions boundary` and still leave the old boundary on `cdk-oerfdev-cfn-exec-role-<account>-<region>`. Confirm with:

```bash
EXEC_ROLE=cdk-oerfdev-cfn-exec-role-906048429638-us-east-1
aws iam get-role --profile Fission-OERF --role-name "$EXEC_ROLE" \
  --query Role.PermissionsBoundary
```

If a boundary ARN is still present when you expect none (or the wrong one), clear it with laptop admin credentials:

```bash
aws iam delete-role-permissions-boundary --profile Fission-OERF --role-name "$EXEC_ROLE"
```

3. **Re-bootstrap with the OERF policies** (section 2b: `OerfDevCfnExecPolicy`, `OerfDevCfnExecPolicy2`, `OerfDevCfnExecDenies`, plus `OerfDevWorkloadBoundary`). The bootstrap template attaches the boundary only when the role is created or updated through that stack; it does **not** always re-attach the boundary after a manual delete. Check again with `get-role`. If `PermissionsBoundary` is missing, attach it:

```bash
aws iam put-role-permissions-boundary --profile Fission-OERF --role-name "$EXEC_ROLE" \
  --permissions-boundary arn:aws:iam::906048429638:policy/OerfDevWorkloadBoundary
```

4. **Verify change sets.** With the same context as section 3, `cdk diff -v` against the four dev stacks must create change sets without `ssm:GetParameters` (or similar) errors on `parameter/cdk-bootstrap/oerfdev/version`. Expect in-place updates only (`PermissionsBoundary`, tags); no replacements of roles, functions, or tables from this change alone.

Changing tags on an existing dev API through API Gateway’s `/tags` path is denied after the OERF policies are in place; a tag-only update rolls that dev stack back and does not change main. Deleting `OerfApiStack-dev` does not delete its CloudFront origin access control; remove that control with laptop admin credentials if the stack delete leaves it behind.

The check is read-only (`iam:SimulatePrincipalPolicy` and `iam:SimulateCustomPolicy` only). Run it after this re-bootstrap and again after the dev application deploy. It exits non-zero on any FAIL.

The channel-router role ARN is the `Role` of `oerf-dev-channel-router`. The phone-number id is the SSM parameter below. The topic ARN is the main inbound topic from the start of this section. Do not paste the results into a ticket; the script prints PASS or FAIL only.

Lines labelled `untagged` pass no resource tag and no request tag. Lines labelled `tagged-policy` are a separate set: they check the tag conditions when the key is present. The evidence analyzer role is the `Role` of `oerf-dev-evidence-analyzer`. The Interpret path calls `bedrock:ApplyGuardrail` from the AgentCore execution role `oerf-dev-agent-runtime`, not from the `oerf-dev-engage-interpret` Lambda role. HTTP and WebSocket API ids are the two `AWS::ApiGatewayV2::Api` resources on the compute stack (`OerfHttpApi` and `OerfWebSocketApi`). The REST API and the distribution are on the API stack. The user pool id is the `CognitoUserPoolId` output of the data stack. Use the `*-dev` stack names for the dev ids and the unsuffixed names for main. Simulation does not require the resource to exist.

```bash
ROUTER_ROLE=$(aws lambda get-function-configuration \
  --profile Fission-OERF \
  --region us-east-1 \
  --function-name oerf-dev-channel-router \
  --query Role \
  --output text)

PHONE_ID=$(aws ssm get-parameter \
  --profile Fission-OERF \
  --region us-east-1 \
  --name /oerf/dev/whatsapp/phone-number-id \
  --query Parameter.Value \
  --output text)

POOL_ID=$(aws cloudformation describe-stacks \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfDataStack \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue | [0]" \
  --output text)

DIST_ID=$(aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfApiStack \
  --query "StackResourceSummaries[?ResourceType=='AWS::CloudFront::Distribution'].PhysicalResourceId | [0]" \
  --output text)

REST_API_ID=$(aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfApiStack \
  --query "StackResourceSummaries[?ResourceType=='AWS::ApiGateway::RestApi'].PhysicalResourceId | [0]" \
  --output text)

DEV_RUNTIME_ARN=$(aws cloudformation describe-stacks \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfComputeStack-dev \
  --query "Stacks[0].Outputs[?OutputKey=='AgentRuntimeArn'].OutputValue | [0]" \
  --output text)

MAIN_RUNTIME_ARN=$(aws cloudformation describe-stacks \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfComputeStack \
  --query "Stacks[0].Outputs[?OutputKey=='AgentRuntimeArn'].OutputValue | [0]" \
  --output text)

DEV_POOL_ID=$(aws cloudformation describe-stacks \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfDataStack-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue | [0]" \
  --output text)

DEV_DIST_ID=$(aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfApiStack-dev \
  --query "StackResourceSummaries[?ResourceType=='AWS::CloudFront::Distribution'].PhysicalResourceId | [0]" \
  --output text)

DEV_REST_API_ID=$(aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfApiStack-dev \
  --query "StackResourceSummaries[?ResourceType=='AWS::ApiGateway::RestApi'].PhysicalResourceId | [0]" \
  --output text)

DEV_GUARDRAIL_ID=$(aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfComputeStack-dev \
  --query "StackResourceSummaries[?ResourceType=='AWS::Bedrock::Guardrail'].PhysicalResourceId | [0]" \
  --output text)

MAIN_GUARDRAIL_ID=$(aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfComputeStack \
  --query "StackResourceSummaries[?ResourceType=='AWS::Bedrock::Guardrail'].PhysicalResourceId | [0]" \
  --output text)

ANALYZER_ROLE=$(aws lambda get-function-configuration \
  --profile Fission-OERF \
  --region us-east-1 \
  --function-name oerf-dev-evidence-analyzer \
  --query Role \
  --output text)

INTERPRET_ROLE=$(aws iam get-role \
  --profile Fission-OERF \
  --role-name oerf-dev-agent-runtime \
  --query Role.Arn \
  --output text)

# HTTP and WebSocket ids. Logical ids start with OerfHttpApi and OerfWebSocketApi.
aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfComputeStack \
  --query "StackResourceSummaries[?ResourceType=='AWS::ApiGatewayV2::Api'].[LogicalResourceId,PhysicalResourceId]" \
  --output text
aws cloudformation list-stack-resources \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfComputeStack-dev \
  --query "StackResourceSummaries[?ResourceType=='AWS::ApiGatewayV2::Api'].[LogicalResourceId,PhysicalResourceId]" \
  --output text

.venv311/bin/python -m scripts.check_dev_boundary \
  --profile Fission-OERF \
  --region us-east-1 \
  --account 906048429638 \
  --router-role-arn "$ROUTER_ROLE" \
  --phone-number-id "$PHONE_ID" \
  --inbound-topic-arn "$SHARED_INBOUND_TOPIC_ARN" \
  --user-pool-arn "arn:aws:cognito-idp:us-east-1:906048429638:userpool/${POOL_ID}" \
  --dev-user-pool-arn "arn:aws:cognito-idp:us-east-1:906048429638:userpool/${DEV_POOL_ID}" \
  --distribution-arn "arn:aws:cloudfront::906048429638:distribution/${DIST_ID}" \
  --dev-distribution-arn "arn:aws:cloudfront::906048429638:distribution/${DEV_DIST_ID}" \
  --rest-api-id "$REST_API_ID" \
  --dev-rest-api-id "$DEV_REST_API_ID" \
  --http-api-id "$MAIN_HTTP_API_ID" \
  --dev-http-api-id "$DEV_HTTP_API_ID" \
  --ws-api-id "$MAIN_WS_API_ID" \
  --dev-ws-api-id "$DEV_WS_API_ID" \
  --caller-role-arn arn:aws:iam::906048429638:role/oerf-dev-agent-caller \
  --dev-runtime-arn "$DEV_RUNTIME_ARN" \
  --main-runtime-arn "$MAIN_RUNTIME_ARN" \
  --dev-guardrail-arn "arn:aws:bedrock:us-east-1:906048429638:guardrail/${DEV_GUARDRAIL_ID}" \
  --main-guardrail-arn "arn:aws:bedrock:us-east-1:906048429638:guardrail/${MAIN_GUARDRAIL_ID}" \
  --analyzer-role-arn "$ANALYZER_ROLE" \
  --interpret-role-arn "$INTERPRET_ROLE"
```

`SHARED_INBOUND_TOPIC_ARN` is the main topic from the `list-stack-resources` command at the top of this section. If `oerf-dev-channel-router` does not exist yet, deploy the four application stacks (section 3) first, then run the check.

### 3. First dev deploy (four app stacks)

Docker must be running (`agentcore:enabled=true` builds the AgentCore image).

Build the desk before `cdk diff`. One production build is used for every stage. CDK writes `/config.json` from this stage's outputs when `desk:publish=true`. Do not bake `VITE_*` into the dev build, and do not `aws s3 sync` the desk by hand for dev.

```bash
cd web
npm ci
npm run build
cd ..
```

Put the dev WhatsApp phone-number id in SSM. This is the End User Messaging **phone-number id** (for example `phone-number-id-…`), not an E.164 phone number. Use a placeholder until a real id exists:

```bash
PLACEHOLDER_PHONE_ID='phone-number-id-01234567890123456789012345678901'

aws ssm put-parameter \
  --profile Fission-OERF \
  --region us-east-1 \
  --name /oerf/dev/whatsapp/phone-number-id \
  --type String \
  --value "$PLACEHOLDER_PHONE_ID" \
  --overwrite
```

Pass the same id as CDK context so IAM resources scope correctly at synth time. On the **first** deploy only, add `-c demo:seedDevices=true`. Later deploys omit that flag. Set `SHARED_INBOUND_TOPIC_ARN` from the `list-stack-resources` command above before `cdk diff`.

```bash
cdk diff \
  --profile Fission-OERF \
  OerfDataStack-dev OerfComputeStack-dev OerfApiStack-dev OerfObservabilityStack-dev \
  -c stage=dev \
  -c account=906048429638 \
  -c auth:managedLogin=true \
  -c agentcore:enabled=true \
  -c dashboard:enabled=true \
  -c desk:publish=true \
  -c channels:live=false \
  -c whatsapp:phoneNumberId="$PLACEHOLDER_PHONE_ID" \
  -c whatsapp:sharedInboundTopicArn="$SHARED_INBOUND_TOPIC_ARN" \
  -c demo:seedDevices=true
```

If these four stacks already exist, the diff from this change should add `PermissionsBoundary` on existing `AWS::IAM::Role` resources and `oerf:stage=dev` on taggable resources. CloudFormation updates both in place and does not replace the role. A replacement of a role, function, or table is not part of this change. The managed policies live in `OerfCiStack-dev`, which this diff does not deploy.

`CreateRestApi` and `CreateApi` send `oerf:stage=dev` on the create call, so the first dev deploy does not call `/tags`. Changing tags on an existing dev API is denied. A later change to those tags rolls that dev stack back and does not change main. Deleting `OerfApiStack-dev` cannot delete the CloudFront origin access control: `UpdateOriginAccessControl` and `DeleteOriginAccessControl` are denied. After the stack rolls back or the other resources are gone, delete that control with the laptop admin credentials.

```bash
cdk deploy \
  --profile Fission-OERF \
  OerfDataStack-dev OerfComputeStack-dev OerfApiStack-dev OerfObservabilityStack-dev \
  -c stage=dev \
  -c account=906048429638 \
  -c auth:managedLogin=true \
  -c agentcore:enabled=true \
  -c dashboard:enabled=true \
  -c desk:publish=true \
  -c channels:live=false \
  -c whatsapp:phoneNumberId="$PLACEHOLDER_PHONE_ID" \
  -c whatsapp:sharedInboundTopicArn="$SHARED_INBOUND_TOPIC_ARN" \
  -c demo:seedDevices=true
```

After the deploy, open `<DashboardUrl>/config.json`. `DashboardUrl` is an output of `OerfApiStack-dev`. `apiUrl` and `userPoolClientId` should be non-empty. Do not paste the file into a ticket or a log. The hand-built `VITE_*` upload is not used for dev. Main keeps the previous desk until a main deploy passes `-c desk:publish=true`.

```bash
aws cloudformation describe-stacks \
  --profile Fission-OERF \
  --region us-east-1 \
  --stack-name OerfApiStack-dev \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue | [0]" \
  --output text
```

GitHub dev deploys run automatically when a merge lands on `develop` (`.github/workflows/deploy.yml` runs CI first, then deploy). Pushes that change only Markdown under `docs/` or `*.md` files do not start a deploy. Manual runs use **Actions → Deploy OERF → Run workflow** with **Use workflow from: develop** only. After this PR merges, set the GitHub **dev** environment branch rule to **develop** only (remove any temporary feature branch).

Each run uses `OerfGitHubDeployRole-dev`, assumes the oerfdev bootstrap roles, and targets the `*-dev` stacks. A push deploy uses `mode=deploy`, AgentCore and the dashboard on, `desk:publish=true`, `demo:seedDevices` off, and `channels:live` from the dev environment variable `CHANNELS_LIVE` (false when unset). A manual run keeps the workflow inputs (default `mode=diff`). When the dashboard flag is true, the workflow builds `web/` and passes `desk:publish=true`.

### 4. IAM simulation (dev deploy role)

After bootstrap, confirm the oerfdev **deploy** role cannot touch main stacks or the protected dev stacks. Replace nothing if the account and region match.

```bash
DEPLOY_ROLE=arn:aws:iam::906048429638:role/cdk-oerfdev-deploy-role-906048429638-us-east-1

# Main stack name (no -dev suffix): no allow → implicitDeny
aws iam simulate-principal-policy \
  --profile Fission-OERF \
  --policy-source-arn "$DEPLOY_ROLE" \
  --action-names cloudformation:UpdateStack \
  --resource-arns arn:aws:cloudformation:us-east-1:906048429638:stack/OerfComputeStack/example

# CI role stack: explicitDeny (DenyOerfCiStack)
aws iam simulate-principal-policy \
  --profile Fission-OERF \
  --policy-source-arn "$DEPLOY_ROLE" \
  --action-names cloudformation:DescribeStacks \
  --resource-arns arn:aws:cloudformation:us-east-1:906048429638:stack/OerfCiStack-dev/example

# Dev toolkit writes: explicitDeny (DenyToolkitWrites)
aws iam simulate-principal-policy \
  --profile Fission-OERF \
  --policy-source-arn "$DEPLOY_ROLE" \
  --action-names cloudformation:ExecuteChangeSet \
  --resource-arns arn:aws:cloudformation:us-east-1:906048429638:changeSet/example/CDKToolkit-dev
```

Expect `EvalDecision` **`implicitDeny`** on `OerfComputeStack`, and **`explicitDeny`** on `OerfCiStack-dev` and `CDKToolkit-dev`. A sanity check that dev app stacks are allowed:

```bash
aws iam simulate-principal-policy \
  --profile Fission-OERF \
  --policy-source-arn "$DEPLOY_ROLE" \
  --action-names cloudformation:UpdateStack \
  --resource-arns arn:aws:cloudformation:us-east-1:906048429638:stack/OerfDataStack-dev/example
```

Expect **`allowed`**.

### 5. Dev synth and deploy context

Every dev `cdk synth`, `cdk diff`, and `cdk deploy` needs:

```text
-c stage=dev -c account=906048429638
```

First full dev deploy (section 3) also passes `-c auth:managedLogin=true`, `-c agentcore:enabled=true`, `-c dashboard:enabled=true`, `-c desk:publish=true`, `-c channels:live=false`, `-c whatsapp:phoneNumberId=<id>`, `-c whatsapp:sharedInboundTopicArn=<main inbound topic ARN>`, and `-c demo:seedDevices=true` once. `desk:publish=true` needs `web/dist` from `cd web && npm ci && npm run build`. Later dev deploys of the dashboard keep `-c auth:managedLogin=true`, `-c desk:publish=true`, and the shared inbound topic ARN. `OerfCiStack-dev` synth adds `-c ci:deployRoleStack=true` and `-c github:oidcProviderArn=<arn>`.

### 6. Common CLI failures

| Symptom | Fix |
| --- | --- |
| `stage' is required ... (got None)` | Add `-c stage=dev` or `-c stage=main` on every `cdk` command. |
| `No module named 'aws_cdk'` | Activate `.venv311` before `cdk` (see top of this section). |
| `InvalidClientTokenId` on `aws sts` | Use `Fission-OERF`, not the default profile; clear stale `AWS_*` session env vars. |
| Explicit deny on `OerfCiStack-dev` during `cdk deploy` | Deploy that stack with `aws cloudformation deploy` as laptop admin (section 2). |
