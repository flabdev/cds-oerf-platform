# Deploy and restore the GitHub deploy role

`OerfGitHubDeployRole` is the role GitHub Actions assumes for `cdk deploy`. SEC-1 narrows it. The first deploy of that change has to run while the live role still has `AdministratorAccess`. After it succeeds, GitHub can no longer widen the role.

The workflow is `.github/workflows/deploy.yml`. It runs only for `refs/heads/main`. Repository variables `AWS_ACCOUNT_ID` and `DEPLOY_ROLE_NAME` (`OerfGitHubDeployRole`) supply the account and the role name. Region is `us-east-1`. The workflow does not print the OIDC token.

## What the role allows

The trust policy allows `sts:AssumeRoleWithWebIdentity` only when both of these are exact:

| Condition | Value |
| --- | --- |
| `token.actions.githubusercontent.com:aud` | `sts.amazonaws.com` |
| `token.actions.githubusercontent.com:sub` | `repo:flabdev@25605151/cds-oerf-platform@1408373494:ref:refs/heads/main` or `repo:flabdev/cds-oerf-platform:ref:refs/heads/main` |

The repository uses an immutable subject prefix `repo:flabdev@25605151/cds-oerf-platform@1408373494`. Check it with `gh api repos/flabdev/cds-oerf-platform/actions/oidc/customization/sub`.

There is no wildcard on the subject. A pull request, a feature branch, and a GitHub Environment use a different `sub`, so they cannot assume the role.

The identity policy has one statement: `sts:AssumeRole` on `arn:aws:iam::<account>:role/cdk-hnb659fds-*-<account>-us-east-1`. That is the CDK bootstrap family (`deploy`, `file-publishing`, `image-publishing`, `lookup`). `cdk deploy` assumes those roles and then calls CloudFormation and SSM as the deploy role. The bootstrap template puts `cloudformation:DescribeStacks` and `ssm:GetParameter` on `/cdk-bootstrap/hnb659fds/version` on `DeploymentActionRole` (sids `CliPermissions` and `ReadVersion`), not on `OerfGitHubDeployRole`. See the [bootstrapping guide](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html) and the [bootstrap template](https://github.com/aws/aws-cdk-cli/blob/main/packages/aws-cdk/lib/api/bootstrap/bootstrap-template.yaml).

The `*` also matches `cdk-hnb659fds-cfn-exec-role-<account>-us-east-1`. That role trusts only `cloudformation.amazonaws.com`. GitHub cannot assume it. The exception is recorded in `docs/security/iam-exceptions.md`.

The account must already be bootstrapped (`CDKToolkit` in `us-east-1`). This role cannot create the bootstrap stack.

## Demo deploy inputs

**Deploy CDK Infrastructure** is `workflow_dispatch` on `main`. CDK context defaults in code stay off. CI synth does not pass these inputs.

| Input | Default | Passed as |
| --- | --- | --- |
| `agentcore` | true | `agentcore:enabled` |
| `dashboard` | true | `dashboard:enabled` |
| `channels_live` | false | `channels:live` |
| `seed_demo_devices` | false | `demo:seedDevices=true` only when true. Tick it only for a deploy that must write the demo `DEVICE#` rows (first demo deploy, or after a reset). |
| `waba_id` | empty | `whatsapp:wabaId` when the resolved value is not empty |
| `phone_number_id` | empty | `whatsapp:phoneNumberId` when the resolved value is not empty |
| `alarm_email` | empty | `alarms:email` when the resolved value is not empty |

An empty `waba_id`, `phone_number_id`, or `alarm_email` uses repository variables `WABA_ID`, `WA_PHONE_NUMBER_ID`, and `ALARM_EMAIL`. If that variable is also empty, the context key is omitted. AgentCore's default builds the ARM64 image.

## DynamoDB encryption

Context `dynamodb:kmsManaged` defaults to `false`. The four tables then omit `encryption` and stay on the AWS owned key. Pass `dynamodb:kmsManaged=true` only to move them to the AWS managed key `aws/dynamodb` (ADR-033). The evidence and report buckets still use SSE-KMS `aws/s3` either way.

A table's encryption mode can change 4 times per 24 hours, then once per 6 hours. Never toggle the flag in a deploy that might roll back. A failed deploy and its rollback each count as a change. Enable it after the demo, in a single deploy, once that window has reset.

## Demo device seed

`seed_demo_devices` writes `DEVICE#{device_id}` / `META` on the routing table from `sim/seed/*/shipments.json`. The row carries `tenant_id`, `shipment_id`, and `asset_id` (`truck`). Ingest only reads that row and still drops an unknown device. The files are the demo tenants `t1`, `t2`, and `sbx`. A second run replaces the same ids.

A normal `OerfComputeStack` deploy does not write those rows. Pass CDK context `demo:seedDevices=true` to add `oerf-device-seed` and the custom resource `DemoDeviceSeed` (`SeedVersion` `1`). The code default is off, so `cdk synth` and CI stay unseeded. On GitHub, **Deploy CDK Infrastructure** defaults `seed_demo_devices` to false. Tick it for the first demo deploy or after a reset. Delete of the resource does not remove the rows.

Unit tests do not prove the deployed table. After a deploy with `demo:seedDevices=true`, confirm `RF-7781` resolves to tenant `t1` and shipment `PH-2841`, and that an id that is not in the seed files does not resolve.

## Demo incident trigger

`POST /incidents/trigger` is retired (interfaces v1.14). The action handler does not call `StartExecution`. Ingest (`sim.reefer` / `TemperatureReading` on EventBridge) is the only start of an incident and its execution. Dashboard actions are `POST /incidents/{id}/actions` and do not write incident state.

Simulator readings must be published with `aws events put-events` (a new EventBridge id per event). A direct Lambda invoke of the same JSON is deduped by `INGEST#` for 7 days, and reset does not clear those rows.

## Demo reset

`python -m scripts.reset` clears one demo tenant so the cold-chain walk can be run again. It is an operator tool. It is not a Lambda and it is not part of CI. `--profile` is required. `--tenant` is `t1`, `t2`, or `sbx`. `--tenant all` is refused, including on a dry run, unless `--yes-all` is also set, and that refusal happens before any AWS call. The default is a dry run. `--yes` deletes. `--reseed` rewrites that tenant's demo devices and, when `PP-2-8-45` is not 45 minutes, publishes the seed cold-chain config for that tenant only.

The command stops that tenant's `RUNNING` executions of `oerf-engage-party` and then `oerf-cold-chain`. An execution is selected by `tenant_id` in its input. It deletes that tenant's incident items, shipment locks (including `ESCALATED`), debounce rows, and the routing rows tied to its engagements. Evidence under `tenants/{tid}/` is deleted. `reports/{tid}/` is the audit record and is kept. The tenant-table counter `TENANT#{tid}` / `COUNTER#INCIDENT` is kept, so the next incident id continues. Tenant config, live websocket rows, `INGEST#` dedupe rows, and the platform table are kept.

```text
make reset-test TENANT=t1 PROFILE=cds-oerf-poc
make reset-demo TENANT=t1 PROFILE=cds-oerf-poc
```

`reset-test` is the dry run. `reset-demo` applies and reseeds. A second apply reports zero for every kind.

## Run the PH-2841 demo

Reset the tenant first. A reset does not clear `INGEST#` rows, so a replayed reading id is ignored for 7 days. Publish a new EventBridge id each time. Direct Lambda invoke of the same JSON is deduped the same way.

1. `make reset-demo TENANT=t1 PROFILE=cds-oerf-poc`
2. Publish one `sim.reefer` / `TemperatureReading` with `aws events put-events`. Use a new `id`. The device `RF-7781` resolves to tenant `t1` and shipment `PH-2841`.
3. Watch `oerf-cold-chain` until `EngageDriverDiagnosis` is waiting on the child, and the child is waiting on `SendViaRouter`.
4. Reply. With channels mocked, `python -m scripts.demo_reply --profile cds-oerf-poc --tenant t1 --shipment PH-2841 --role driver --button running`. With `channels:live` true, the driver replies on WhatsApp to template `oerf_driver_checkin`. The script reads the address from SSM `/oerf/demo-numbers/t1/driver` and prints the shipment and the message id, not the number.
5. The child ends `answered` / `unit_running`. The parent takes the default of `NeedApproval?` into `SecureFacility` (still the stub), then real `Act`. The next state is `VerifyLoop`, which is unchanged and does not close this excursion by itself.

`demo_reply` only posts a WhatsApp provider message to the inbound queue. It does not send a dashboard action and it does not call `SendTaskSuccess`.

After each rehearsal, cancel the incident from the desk or run `make reset-demo TENANT=t1 PROFILE=cds-oerf-poc`.

## First deploy from main

Do this once, from `main`, before any later deploy uses the narrowed role.

1. Merge the SEC-1 commit to `main`.
2. Confirm the live role still has `AdministratorAccess`. If it does not, use the restore steps below before trying again.
3. In GitHub, run **Deploy CDK Infrastructure** on `main`. The defaults turn AgentCore and the dashboard on and leave live channels and demo device seeding off; tick `seed_demo_devices` for the first demo deploy or after a reset. `waba_id`, `phone_number_id`, and `alarm_email` are empty unless this release sets them; an empty input uses repository variables `WABA_ID`, `WA_PHONE_NUMBER_ID`, and `ALARM_EMAIL`. A variable that is also empty omits that context key. CDK context defaults in code stay off, so a local synth does not turn these on.
4. Read the `cdk diff` step. The data stack change replaces `AdministratorAccess` with the bootstrap `AssumeRole` statement and tightens the trust.
5. Wait until the deploy finishes. A second run waits on the concurrency group `cdk-deploy-us-east-1`.

## If deploy breaks

GitHub Actions cannot put `AdministratorAccess` back. Restore from a laptop with an IAM principal that can update `OerfDataStack`. Do not use `OerfGitHubDeployRole` for the restore.

1. If the stack is stuck in `UPDATE_ROLLBACK_FAILED`, continue the rollback in CloudFormation for `OerfDataStack` first. The admin principal does that. The GitHub role cannot.
2. Check out the parent of the commit that removed `AdministratorAccess` from `OerfGitHubDeployRole`.
3. Export admin credentials in the shell. Do not commit them and do not echo them.
4. From the repository root, with the account id from `AWS_ACCOUNT_ID`:

```text
CDK_DEFAULT_ACCOUNT=<account-id> CDK_DEFAULT_REGION=us-east-1 cdk deploy --all
```

5. Confirm `OerfGitHubDeployRole` has `AdministratorAccess` again and the old trust policy. Fix the failure, then repeat the first deploy from `main`.

`cdk diff` in the workflow uses `--fail-on never`, so a non-empty change set does not by itself fail the job. An access error after the role has been narrowed does. That is the case for this restore.
