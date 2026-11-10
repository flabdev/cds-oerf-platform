"""Credential-free skip, or a read-only check of the dev stage.

    python -m scripts.smoke_dev

prints ``skipped`` and exits 0 when ``AWS_PROFILE`` is unset, so CI can import
the module without a deploy. With a profile set it reads stack status only.
It does not print parameters, phone numbers, or tokens.

    AWS_PROFILE=Fission-OERF python -m scripts.smoke_dev
"""

from __future__ import annotations

import os

_STACKS = (
    "OerfDataStack-dev",
    "OerfComputeStack-dev",
    "OerfApiStack-dev",
    "OerfObservabilityStack-dev",
)


def main() -> int:
    profile = os.environ.get("AWS_PROFILE", "").strip()
    if not profile:
        print("skipped: set AWS_PROFILE to check the dev stacks")
        return 0
    import boto3

    client = boto3.Session(profile_name=profile, region_name="us-east-1").client(
        "cloudformation"
    )
    for name in _STACKS:
        described = client.describe_stacks(StackName=name)
        status = described["Stacks"][0]["StackStatus"]
        if not status.endswith("COMPLETE") or "ROLLBACK" in status:
            print(f"FAIL {name} {status}")
            return 1
        print(f"PASS {name} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
