"""Create one demo dashboard user in the OERF user pool.

Operator tool. Never runs in Lambda or CI. The default is a dry run; ``--yes``
applies it. A generated password is printed once and is not written to a file.

    python -m scripts.seed_demo_users \\
        --profile cds-oerf-poc --stack OerfDataStack \\
        --tenant TENANT --role ops_user --email ops@example.com

    python -m scripts.seed_demo_users \\
        --profile cds-oerf-poc --user-pool-id POOL \\
        --tenant TENANT --role tenant_admin --email admin@example.com --yes

The email is the Cognito username and must be on example.com, example.org, or
example.net. ``--password-env NAME`` uses that variable instead of generating
a password, and that value is not printed. Region is us-east-1. The pool id
comes from ``--user-pool-id`` or the ``CognitoUserPoolId`` output of
``--stack`` (OerfDataStack). Group names are the roles.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import string
import sys
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

from oerf.platform.api.claims import TENANT_CLAIM

REGION = "us-east-1"
POOL_OUTPUT = "CognitoUserPoolId"
ROLES = ("platform_admin", "tenant_admin", "ops_user")
MIN_PASSWORD = 12
_SYMBOLS = "!@#$%^&*"
_EMAIL = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}@(example\.com|example\.org|example\.net)$")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class SeedRequest:
    """One user to create or align with a tenant and a single group."""

    user_pool_id: str
    tenant_id: str
    role: str
    email: str
    apply: bool
    password: str | None
    reveal_password: bool


@dataclass(frozen=True)
class SeedResult:
    """What the call did, or what a dry run would do. ``password`` is set only to print."""

    action: str
    applied: bool
    password: str | None
    tenant_write: bool
    group_add: bool
    groups_removed: tuple[str, ...]


def password_meets_policy(password: str) -> bool:
    """Pool policy: at least 12 characters with upper, lower, digit, and symbol."""

    return (
        len(password) >= MIN_PASSWORD
        and any(char.isupper() for char in password)
        and any(char.islower() for char in password)
        and any(char.isdigit() for char in password)
        and any(not char.isalnum() for char in password)
    )


def generate_password(length: int = 16) -> str:
    """A password from ``secrets`` that meets the pool policy."""

    if length < MIN_PASSWORD:
        raise ValueError("length")
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(_SYMBOLS),
    ]
    alphabet = string.ascii_letters + string.digits + _SYMBOLS
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
    for index in range(len(chars) - 1, 0, -1):
        swap = secrets.randbelow(index + 1)
        chars[index], chars[swap] = chars[swap], chars[index]
    password = "".join(chars)
    if not password_meets_policy(password):
        raise RuntimeError("generated password failed the pool policy")
    return password


def user_pool_id(cloudformation: object, stack: str) -> str:
    """Read ``CognitoUserPoolId`` from a stack's outputs."""

    response = cloudformation.describe_stacks(StackName=stack)  # type: ignore[attr-defined]
    outputs = response["Stacks"][0].get("Outputs") or []
    for item in outputs:
        if item.get("OutputKey") == POOL_OUTPUT:
            value = str(item.get("OutputValue") or "").strip()
            if value:
                return value
    raise LookupError(POOL_OUTPUT)


def seed_user(cognito: object, request: SeedRequest) -> SeedResult:
    """Create the user or make the stored tenant and group match the request."""

    _check_request(request)
    current = _existing_user(cognito, request.user_pool_id, request.email)
    if current is None:
        groups: list[str] = []
        tenant_now = None
    else:
        tenant_now = _attribute(current, TENANT_CLAIM)
        groups = _groups(cognito, request.user_pool_id, request.email)
    extra = tuple(name for name in dict.fromkeys(groups) if name != request.role)
    missing_group = request.role not in groups
    tenant_write = current is None or tenant_now != request.tenant_id
    if current is None:
        action = "create"
    elif tenant_write or missing_group or extra:
        action = "update"
    else:
        action = "unchanged"
    if not request.apply:
        return SeedResult(action, False, None, tenant_write, missing_group, extra)

    revealed: str | None = None
    if current is None:
        password = request.password if request.password is not None else generate_password()
        if not password_meets_policy(password):
            raise ValueError("password does not meet the pool policy")
        cognito.admin_create_user(  # type: ignore[attr-defined]
            UserPoolId=request.user_pool_id,
            Username=request.email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": request.email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": TENANT_CLAIM, "Value": request.tenant_id},
            ],
        )
        cognito.admin_set_user_password(  # type: ignore[attr-defined]
            UserPoolId=request.user_pool_id,
            Username=request.email,
            Password=password,
            Permanent=True,
        )
        if request.reveal_password:
            revealed = password
    elif tenant_now != request.tenant_id:
        cognito.admin_update_user_attributes(  # type: ignore[attr-defined]
            UserPoolId=request.user_pool_id,
            Username=request.email,
            UserAttributes=[{"Name": TENANT_CLAIM, "Value": request.tenant_id}],
        )
    for name in extra:
        cognito.admin_remove_user_from_group(  # type: ignore[attr-defined]
            UserPoolId=request.user_pool_id,
            Username=request.email,
            GroupName=name,
        )
    if missing_group:
        cognito.admin_add_user_to_group(  # type: ignore[attr-defined]
            UserPoolId=request.user_pool_id,
            Username=request.email,
            GroupName=request.role,
        )
    return SeedResult(action, True, revealed, tenant_write, missing_group, extra)


def main(
    argv: list[str] | None = None,
    *,
    cognito: object | None = None,
    cloudformation: object | None = None,
) -> int:
    """Parse arguments, then dry-run or apply. Injected clients skip the profile session."""

    args = _parser().parse_args(argv)
    email = args.email.strip().lower()
    tenant = args.tenant.strip()
    problem = _input_problem(email, tenant)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    password, reveal, problem = _password_from_env(args.password_env)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    try:
        pool, client = _clients(args, cognito, cloudformation)
        result = seed_user(
            client,
            SeedRequest(
                user_pool_id=pool,
                tenant_id=tenant,
                role=args.role,
                email=email,
                apply=args.yes,
                password=password,
                reveal_password=reveal,
            ),
        )
    except (ClientError, LookupError, ValueError) as exc:
        print(_failure(exc), file=sys.stderr)
        return 1
    _report(result, email, tenant, args.role)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create one demo Cognito user. Dry run unless --yes.",
    )
    parser.add_argument("--profile", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--user-pool-id")
    target.add_argument("--stack", help="Stack whose CognitoUserPoolId output is the pool")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password-env", help="Environment variable holding the password")
    parser.add_argument("--dry-run", action="store_true", help="Report only (the default)")
    parser.add_argument("--yes", action="store_true", help="Create or update the user")
    return parser


def _clients(
    args: argparse.Namespace,
    cognito: object | None,
    cloudformation: object | None,
) -> tuple[str, object]:
    if args.yes and args.dry_run:
        raise ValueError("pass either --yes or --dry-run")
    if cognito is not None and (args.user_pool_id or cloudformation is not None):
        client = cognito
        formed = cloudformation
    else:
        session = boto3.Session(profile_name=args.profile, region_name=REGION)
        client = cognito or session.client("cognito-idp")
        formed = cloudformation or session.client("cloudformation")
    if args.user_pool_id:
        return args.user_pool_id, client
    return user_pool_id(formed, args.stack), client


def _password_from_env(name: str | None) -> tuple[str | None, bool, str | None]:
    if not name:
        return None, True, None
    value = os.environ.get(name)
    if not value:
        return None, False, "password environment variable is empty"
    if not password_meets_policy(value):
        return None, False, "password does not meet the pool policy"
    return value, False, None


def _input_problem(email: str, tenant: str) -> str | None:
    if _EMAIL.fullmatch(email) is None:
        return "email must be on example.com, example.org, or example.net"
    if _TENANT.fullmatch(tenant) is None:
        return "tenant must be 1-128 letters, digits, dots, underscores, or hyphens"
    return None


def _check_request(request: SeedRequest) -> None:
    if request.role not in ROLES:
        raise ValueError("role")
    if _input_problem(request.email, request.tenant_id):
        raise ValueError("email or tenant")
    if not request.user_pool_id.strip():
        raise ValueError("user pool id")


def _existing_user(cognito: object, pool: str, email: str) -> dict[str, object] | None:
    try:
        user = cognito.admin_get_user(UserPoolId=pool, Username=email)  # type: ignore[attr-defined]
    except ClientError as exc:
        if _code(exc) == "UserNotFoundException":
            return None
        raise
    return user


def _groups(cognito: object, pool: str, email: str) -> list[str]:
    names: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, str] = {"UserPoolId": pool, "Username": email}
        if token:
            kwargs["NextToken"] = token
        page = cognito.admin_list_groups_for_user(**kwargs)  # type: ignore[attr-defined]
        names.extend(
            str(group["GroupName"]) for group in page.get("Groups", []) if group.get("GroupName")
        )
        token = page.get("NextToken")
        if not token:
            return names


def _attribute(user: dict[str, object], name: str) -> str | None:
    raw = user.get("UserAttributes")
    if not isinstance(raw, list):
        return None
    for item in raw:
        if isinstance(item, dict) and item.get("Name") == name:
            value = item.get("Value")
            return str(value) if value is not None else None
    return None


def _code(exc: ClientError) -> str:
    error = exc.response.get("Error")
    if isinstance(error, dict):
        return str(error.get("Code") or "error")
    return "error"


def _failure(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return f"cognito {_code(exc)}"
    if isinstance(exc, LookupError):
        return "CognitoUserPoolId output is missing"
    return "password does not meet the pool policy"


def _report(result: SeedResult, email: str, tenant: str, role: str) -> None:
    mode = "applied" if result.applied else "dry-run"
    print(f"{mode} {result.action} {email} tenant={tenant} group={role}")
    if result.groups_removed:
        label = "removed" if result.applied else "would remove"
        print(f"{label} {' '.join(result.groups_removed)}")
    if result.password:
        print(result.password)


if __name__ == "__main__":
    raise SystemExit(main())
