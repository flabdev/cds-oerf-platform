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

## Demo device seed

`seed_demo_devices` writes `DEVICE#{device_id}` / `META` on the routing table from `sim/seed/*/shipments.json`. The row carries `tenant_id`, `shipment_id`, and `asset_id` (`truck`). Ingest only reads that row and still drops an unknown device. The files are the demo tenants `t1`, `t2`, and `sbx`. A second run replaces the same ids.

The deploy writes those rows only when CDK context `demo:seedDevices` is `true`. The default deploy does not. Passing the flag adds `oerf-device-seed` and a custom resource `DemoDeviceSeed` (`SeedVersion` `1`) on `OerfComputeStack`. Delete of that resource does not remove the rows.

Unit tests do not prove the deployed table. After a deploy with the flag, confirm `RF-7781` resolves to tenant `t1` and shipment `PH-2841`, and that an id that is not in the seed files does not resolve.

## Demo incident trigger

`POST /incidents/trigger` on `oerf-http-api` stays as a demo hook. It requires a Cognito token, takes the tenant from `custom:tenant_id`, ignores a tenant in the body, and writes one item: `TENANT#{tenant}#INC#{id}` / `META`. It then calls `StartExecution`. Frozen ingest remains the EventBridge path. Dashboard actions are `POST /incidents/{id}/actions` and do not write incident state.

## First deploy from main

Do this once, from `main`, before any later deploy uses the narrowed role.

1. Merge the SEC-1 commit to `main`.
2. Confirm the live role still has `AdministratorAccess`. If it does not, use the restore steps below before trying again.
3. In GitHub, run **Deploy CDK Infrastructure** on `main`. Leave `agentcore`, `dashboard`, and `channels_live` off, and leave `waba_id` empty, unless this release is meant to turn one of those on.
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
