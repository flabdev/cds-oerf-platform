"""Pre-token trigger copies custom:tenant_id and groups onto the ID token."""

import pytest

from oerf.platform.api.claims import TENANT_CLAIM as CLAIMS_TENANT
from oerf.platform.auth.pre_token import TENANT_CLAIM, apply_tenant_claim, handler

pytestmark = pytest.mark.security


def test_claim_name_matches_the_authorizer_contract() -> None:
    assert TENANT_CLAIM == CLAIMS_TENANT == "custom:tenant_id"


def test_copies_tenant_onto_access_and_id_tokens() -> None:
    event = {
        "request": {"userAttributes": {TENANT_CLAIM: " t1 "}},
        "response": {"claimsAndScopeOverrideDetails": None},
    }

    result = handler(event, None)

    for section in ("accessTokenGeneration", "idTokenGeneration"):
        claims = result["response"]["claimsAndScopeOverrideDetails"][section]
        assert claims["claimsToAddOrOverride"][TENANT_CLAIM] == "t1"


def test_leaves_the_event_unchanged_when_the_attribute_is_missing() -> None:
    event = {"request": {"userAttributes": {"sub": "user-1"}}, "response": {}}

    assert apply_tenant_claim(event) == event
    assert "claimsAndScopeOverrideDetails" not in event["response"]


def test_keeps_claims_already_present_on_the_response() -> None:
    event = {
        "request": {"userAttributes": {TENANT_CLAIM: "t1"}},
        "response": {
            "claimsAndScopeOverrideDetails": {
                "accessTokenGeneration": {
                    "claimsToAddOrOverride": {"custom:other": "kept"},
                }
            }
        },
    }

    result = apply_tenant_claim(event)
    access = result["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]

    assert access["claimsToAddOrOverride"] == {
        "custom:other": "kept",
        TENANT_CLAIM: "t1",
    }


def test_copies_groups_onto_the_id_token() -> None:
    event = {
        "request": {
            "userAttributes": {TENANT_CLAIM: "t1"},
            "groupConfiguration": {
                "groupsToOverride": [" ops_user ", ""],
                "iamRolesToOverride": ["ops-role"],
                "preferredRole": " ops-role ",
            },
        },
        "response": {},
    }

    result = handler(event, None)
    details = result["response"]["claimsAndScopeOverrideDetails"]
    identity = details["idTokenGeneration"]["claimsToAddOrOverride"]

    assert identity[TENANT_CLAIM] == "t1"
    assert "cognito:groups" not in identity
    assert details["groupOverrideDetails"] == {
        "groupsToOverride": ["ops_user"],
        "iamRolesToOverride": ["ops-role"],
        "preferredRole": "ops-role",
    }


def test_groups_are_copied_when_the_tenant_attribute_is_missing() -> None:
    event = {
        "request": {
            "userAttributes": {},
            "groupConfiguration": {"groupsToOverride": ["tenant_admin"]},
        },
        "response": {},
    }

    result = apply_tenant_claim(event)
    details = result["response"]["claimsAndScopeOverrideDetails"]

    assert "idTokenGeneration" not in details
    assert details["groupOverrideDetails"]["groupsToOverride"] == ["tenant_admin"]


def test_empty_groups_do_not_suppress_the_claim() -> None:
    event = {
        "request": {
            "userAttributes": {TENANT_CLAIM: "t1"},
            "groupConfiguration": {"groupsToOverride": [" ", ""]},
        },
        "response": {},
    }

    result = apply_tenant_claim(event)

    assert "groupOverrideDetails" not in result["response"]["claimsAndScopeOverrideDetails"]
