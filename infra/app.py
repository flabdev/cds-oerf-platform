import os

import aws_cdk as cdk

from infra.stacks.data_stack import OerfDataStack

# The CDK CLI exports CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION to the app process.
# The account is only exported when the CLI can resolve credentials, so fail with
# an actionable message instead of a bare KeyError when no profile/credentials
# are active.
account = os.environ.get("CDK_DEFAULT_ACCOUNT")
if not account:
    raise SystemExit(
        "CDK_DEFAULT_ACCOUNT is not set: the CDK CLI could not resolve an AWS "
        "account for this app. Pass an AWS profile to the CDK CLI "
        "(for example `cdk deploy --profile oerf-dev`) or export AWS_PROFILE, "
        "then retry."
    )

app = cdk.App()

OerfDataStack(
    app,
    "OerfDataStack",
    env=cdk.Environment(
        account=account,
        region=os.environ.get(
            "CDK_DEFAULT_REGION",
            "us-east-1",
        ),
    ),
)

app.synth()
