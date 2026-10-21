"""Pre-token trigger copies custom:tenant_id onto both Cognito tokens."""

from oerf.platform.api.claims import TENANT_CLAIM as CLAIMS_TENANT
from oerf.platform.auth.pre_token import TENANT_CLAIM, apply_tenant_claim, handler


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
