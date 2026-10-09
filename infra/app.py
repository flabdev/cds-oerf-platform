import os

import aws_cdk as cdk

from infra.stacks.data_stack import OerfDataStack

app = cdk.App()

OerfDataStack(
    app,
    "OerfDataStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ.get(
            "CDK_DEFAULT_REGION",
            "ap-south-1",
        ),
    ),
)

app.synth()
