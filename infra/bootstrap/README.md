# Dev CDK bootstrap

Main stays on the default bootstrap: qualifier `hnb659fds`, toolkit stack `CDKToolkit`. Dev uses a second bootstrap so its deploy role cannot change main's stacks. The file-publishing, image-publishing, and lookup roles are the stock ones. Their names follow the qualifier, so the asset bucket is `cdk-oerfdev-assets-<account>-<region>` and the repository is `cdk-oerfdev-container-assets-<account>-<region>`.

`dev-bootstrap.yaml` is the template from `cdk bootstrap --show-template` for CLI 2.1145.0. `CdkBootstrapVersion` is **32**. The stock `BootstrapVariant` was `AWS CDK: Default Resources`. This copy sets it to `OERF dev: CloudFormation deploy role limited to *-dev stacks` so the CLI will not overwrite the stack with the stock template.

What changed from that stock template:

- The deploy role's CloudFormation actions allow `stack/*-dev/*` and `changeSet/*/*-dev*`. Change-set calls are authorized against the change set ARN, not only the stack ARN. `CDKToolkit-dev` is not in that allow list.
- `DescribeStacks`, `ListStacks`, `ValidateTemplate`, and `GetTemplateSummary` stay on `Resource: *`. They have no stack to name, or the CLI calls them without one. A deploy reads the toolkit stack only through that `DescribeStacks` allow and through `ssm:GetParameter` on `/cdk-bootstrap/oerfdev/version`.
- An explicit deny blocks every `cloudformation:*` action on `stack/OerfCiStack-dev/*` and its change sets. A second deny blocks CloudFormation writes on `stack/CDKToolkit-dev/*` and its change sets. `*-dev` would otherwise include the toolkit stack. Ananda changes `CDKToolkit-dev` and `OerfCiStack-dev` only from his laptop.
- `AWSCloudFormationReadOnlyAccess` is not attached. It would have allowed reads of every stack, including main.
- `iam:PassRole` is still only `${CloudFormationExecutionRole}`. With qualifier `oerfdev` that is `cdk-oerfdev-cfn-exec-role-<account>-<region>`.
- The execution role still takes `CloudFormationExecutionPolicies`. Empty, and with no trusted accounts, that is `AdministratorAccess`. A permissions boundary is not attached.

Bootstrap it only as stack `CDKToolkit-dev` with `--qualifier oerfdev` and no `--trust`. Do not apply this file to `CDKToolkit`.

When the CDK CLI is upgraded, run `cdk bootstrap --show-template`, diff it against this file, and re-apply only the CloudFormation limits above. Keep `BootstrapVariant` custom so the new stock template does not replace this one. Bump the recorded version if `CdkBootstrapVersion` changes.
