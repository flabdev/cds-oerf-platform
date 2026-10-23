"""The demo-user seeder writes only when asked and does not store a password."""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
from botocore.stub import Stubber

from oerf.platform.api.claims import DEFAULT_ROLE, PLATFORM_ADMIN_GROUP, TENANT_CLAIM
from scripts.seed_demo_users import (
    ROLES,
    SeedRequest,
    generate_password,
    main,
    password_meets_policy,
    seed_user,
    user_pool_id,
)

POOL = "us-east-1_pool"
TENANT = "tenant-a"
EMAIL = "ops@example.com"
PASSWORD = "Aa1!aaaaaaaa"


def test_roles_match_the_pool_groups() -> None:
    assert set(ROLES) == {PLATFORM_ADMIN_GROUP, "tenant_admin", DEFAULT_ROLE}
    assert TENANT_CLAIM == "custom:tenant_id"


def test_generated_password_meets_the_pool_policy() -> None:
    issued = [generate_password() for _ in range(30)]
    assert len(set(issued)) > 1
    for password in issued:
        assert password_meets_policy(password)
    assert not password_meets_policy("short")
    assert not password_meets_policy("alllowercase12")
    assert not password_meets_policy("NoSymbol12345")
    assert password_meets_policy(PASSWORD)


def test_new_user_is_created_suppressed_with_a_permanent_password() -> None:
    client, stubber = _cognito()
    stubber.add_client_error(
        "admin_get_user",
        service_error_code="UserNotFoundException",
        expected_params={"UserPoolId": POOL, "Username": EMAIL},
    )
    stubber.add_response(
        "admin_create_user",
        {"User": {"Username": EMAIL}},
        {
            "UserPoolId": POOL,
            "Username": EMAIL,
            "MessageAction": "SUPPRESS",
            "UserAttributes": [
                {"Name": "email", "Value": EMAIL},
                {"Name": "email_verified", "Value": "true"},
                {"Name": TENANT_CLAIM, "Value": TENANT},
            ],
        },
    )
    stubber.add_response(
        "admin_set_user_password",
        {},
        {"UserPoolId": POOL, "Username": EMAIL, "Password": PASSWORD, "Permanent": True},
    )
    stubber.add_response(
        "admin_add_user_to_group",
        {},
        {"UserPoolId": POOL, "Username": EMAIL, "GroupName": "ops_user"},
    )
    stubber.activate()

    result = seed_user(client, _request(apply=True, password=PASSWORD, reveal=False))

    assert result.action == "create"
    assert result.applied is True
    assert result.password is None
    stubber.assert_no_pending_responses()


def test_existing_user_is_not_given_a_new_password() -> None:
    client, stubber = _cognito()
    stubber.add_response(
        "admin_get_user",
        _user("other-tenant"),
        {"UserPoolId": POOL, "Username": EMAIL},
    )
    stubber.add_response(
        "admin_list_groups_for_user",
        {"Groups": [{"GroupName": "platform_admin"}]},
        {"UserPoolId": POOL, "Username": EMAIL},
    )
    stubber.add_response(
        "admin_update_user_attributes",
        {},
        {
            "UserPoolId": POOL,
            "Username": EMAIL,
            "UserAttributes": [{"Name": TENANT_CLAIM, "Value": TENANT}],
        },
    )
    stubber.add_response(
        "admin_remove_user_from_group",
        {},
        {"UserPoolId": POOL, "Username": EMAIL, "GroupName": "platform_admin"},
    )
    stubber.add_response(
        "admin_add_user_to_group",
        {},
        {"UserPoolId": POOL, "Username": EMAIL, "GroupName": "ops_user"},
    )
    stubber.activate()

    result = seed_user(client, _request(apply=True, password=PASSWORD, reveal=True))

    assert result.action == "update"
    assert result.password is None
    assert result.groups_removed == ("platform_admin",)
    stubber.assert_no_pending_responses()


def test_group_already_set_makes_no_group_write() -> None:
    client, stubber = _cognito()
    stubber.add_response(
        "admin_get_user",
        _user(TENANT),
        {"UserPoolId": POOL, "Username": EMAIL},
    )
    stubber.add_response(
        "admin_list_groups_for_user",
        {"Groups": [{"GroupName": "ops_user"}]},
        {"UserPoolId": POOL, "Username": EMAIL},
    )
    stubber.activate()

    result = seed_user(client, _request(apply=True))

    assert result.action == "unchanged"
    assert result.group_add is False
    assert result.groups_removed == ()
    assert result.password is None
    stubber.assert_no_pending_responses()


def test_dry_run_makes_no_write_calls() -> None:
    missing, missing_stub = _cognito()
    missing_stub.add_client_error(
        "admin_get_user",
        service_error_code="UserNotFoundException",
        expected_params={"UserPoolId": POOL, "Username": EMAIL},
    )
    missing_stub.activate()
    created = seed_user(missing, _request(apply=False, password=PASSWORD))
    assert created.action == "create"
    assert created.applied is False
    assert created.password is None
    missing_stub.assert_no_pending_responses()

    present, present_stub = _cognito()
    present_stub.add_response(
        "admin_get_user",
        _user("other-tenant"),
        {"UserPoolId": POOL, "Username": EMAIL},
    )
    present_stub.add_response(
        "admin_list_groups_for_user",
        {"Groups": [{"GroupName": "platform_admin"}]},
        {"UserPoolId": POOL, "Username": EMAIL},
    )
    present_stub.activate()
    planned = seed_user(present, _request(apply=False))
    assert planned.action == "update"
    assert planned.applied is False
    assert planned.tenant_write is True
    assert planned.group_add is True
    present_stub.assert_no_pending_responses()


def test_environment_password_is_set_once_and_not_printed(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DEMO_USER_PASSWORD", PASSWORD)
    client, stubber = _cognito()
    stubber.add_client_error(
        "admin_get_user",
        service_error_code="UserNotFoundException",
        expected_params={"UserPoolId": POOL, "Username": EMAIL},
    )
    stubber.add_response("admin_create_user", {"User": {"Username": EMAIL}})
    stubber.add_response(
        "admin_set_user_password",
        {},
        {"UserPoolId": POOL, "Username": EMAIL, "Password": PASSWORD, "Permanent": True},
    )
    stubber.add_response("admin_add_user_to_group", {})
    stubber.activate()

    code = main(_args("--yes", "--password-env", "DEMO_USER_PASSWORD"), cognito=client)
    text = capsys.readouterr().out

    assert code == 0
    assert PASSWORD not in text
    assert "applied create" in text
    stubber.assert_no_pending_responses()


def test_dry_run_command_prints_no_password(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "scripts.seed_demo_users.generate_password",
        lambda: (_ for _ in ()).throw(AssertionError("password")),
    )
    client, stubber = _cognito()
    stubber.add_client_error("admin_get_user", service_error_code="UserNotFoundException")
    stubber.activate()

    code = main(_args(), cognito=client)
    text = capsys.readouterr().out

    assert code == 0
    assert "dry-run create" in text
    assert "password" not in text.lower()
    stubber.assert_no_pending_responses()


def test_yes_prints_a_generated_password_once(monkeypatch, capsys) -> None:
    monkeypatch.setattr("scripts.seed_demo_users.generate_password", lambda: PASSWORD)
    client, stubber = _cognito()
    stubber.add_client_error("admin_get_user", service_error_code="UserNotFoundException")
    stubber.add_response("admin_create_user", {"User": {"Username": EMAIL}})
    stubber.add_response("admin_set_user_password", {})
    stubber.add_response("admin_add_user_to_group", {})
    stubber.activate()

    code = main(_args("--yes"), cognito=client)
    text = capsys.readouterr().out

    assert code == 0
    assert text.count(PASSWORD) == 1
    stubber.assert_no_pending_responses()


def test_stack_output_is_the_cognito_pool_id() -> None:
    formed = boto3.client("cloudformation", region_name="us-east-1")
    stubber = Stubber(formed)
    stubber.add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": "OerfDataStack",
                    "CreationTime": datetime(2026, 10, 23, tzinfo=UTC),
                    "StackStatus": "UPDATE_COMPLETE",
                    "Outputs": [
                        {"OutputKey": "TenantTableName", "OutputValue": "other"},
                        {"OutputKey": "CognitoUserPoolId", "OutputValue": POOL},
                    ],
                }
            ]
        },
        {"StackName": "OerfDataStack"},
    )
    stubber.activate()

    assert user_pool_id(formed, "OerfDataStack") == POOL
    stubber.assert_no_pending_responses()


def test_a_real_email_domain_is_rejected_before_any_call() -> None:
    client, stubber = _cognito()
    stubber.activate()

    code = main(_args("--yes", "--email", "person@gmail.com"), cognito=client)

    assert code == 2
    stubber.assert_no_pending_responses()


def _request(*, apply: bool, password: str | None = None, reveal: bool = False) -> SeedRequest:
    return SeedRequest(POOL, TENANT, "ops_user", EMAIL, apply, password, reveal)


def _args(*extra: str, email: str = EMAIL) -> list[str]:
    return [
        "--profile",
        "demo",
        "--user-pool-id",
        POOL,
        "--tenant",
        TENANT,
        "--role",
        "ops_user",
        "--email",
        email,
        *extra,
    ]


def _user(tenant: str) -> dict[str, object]:
    return {
        "Username": EMAIL,
        "UserAttributes": [
            {"Name": "email", "Value": EMAIL},
            {"Name": TENANT_CLAIM, "Value": tenant},
        ],
    }


def _cognito() -> tuple[object, Stubber]:
    client = boto3.client("cognito-idp", region_name="us-east-1")
    return client, Stubber(client)
