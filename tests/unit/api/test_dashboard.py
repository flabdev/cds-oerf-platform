"""Tenant isolation, SQS actions, and the T2 allowance publish flow."""

from datetime import UTC, datetime

import pytest

from oerf.platform.api.dashboard import (
    DashboardServices,
    MemoryActionQueue,
    MemoryAuditLog,
    MemoryCatalog,
    MemoryIncidentQuery,
    Principal,
    dispatch,
    handler,
)
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from oerf.platform.tenancy.publisher import ConfigPublisher
from tests.unit.support.fake_dynamo import FakeDynamoClient

CLOCK = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


@pytest.mark.security
def test_incident_list_and_detail_stay_inside_the_claim_tenant() -> None:
    services = _services()
    t1 = Principal("t1", "ops_user", "user-1", True)

    listed = dispatch("GET", "/incidents", t1, {"tenant_id": "t2"}, services)
    detail = dispatch("GET", "/incidents/INC-2", t1, None, services)
    own = dispatch("GET", "/incidents/INC-1", t1, None, services)

    assert listed[0] == 403
    assert detail == (404, {"error": "not_found"})
    assert own[1]["shipment_id"] == "PH-2841"
    assert own[1]["tenant_id"] == "t1"
    assert "pk" not in own[1]


@pytest.mark.security
def test_unauthenticated_request_is_rejected() -> None:
    response = handler({"httpMethod": "GET", "path": "/incidents"}, None)

    assert response["statusCode"] == 401


@pytest.mark.security
def test_action_is_queued_once_and_carries_no_task_token() -> None:
    services = _services()
    principal = Principal("t1", "ops_user", "user-1", True)
    body = {"action": "confirm_transfer", "action_id": "act-1", "engagement_id": "ENG-03"}

    first = dispatch(
        "POST", "/incidents/INC-1/actions", principal, body, services, idempotency_key="act-1"
    )
    second = dispatch(
        "POST", "/incidents/INC-1/actions", principal, body, services, idempotency_key="act-1"
    )

    assert first == (202, {"action_id": "act-1", "status": "queued"})
    assert second[1]["action_id"] == first[1]["action_id"]
    assert len(services.queue.messages) == 1
    assert services.queue.messages[0]["action"] == "confirm_transfer"
    assert "task_token" not in services.queue.messages[0]


@pytest.mark.security
def test_ops_user_cannot_publish_and_admin_publish_is_audited() -> None:
    services = _services()
    ops = Principal("t2", "ops_user", "ops-1", True)
    admin = Principal("t2", "tenant_admin", "admin-1", True)
    draft = _profiles(30)

    forbidden = dispatch(
        "PUT",
        "/admin/tenants/t2/domains/cold_chain_excursion/config",
        ops,
        draft,
        services,
    )
    saved = dispatch(
        "PUT",
        "/admin/tenants/t2/domains/cold_chain_excursion/config",
        admin,
        draft,
        services,
    )
    published = dispatch(
        "POST",
        "/admin/tenants/t2/domains/cold_chain_excursion/config/publish",
        admin,
        None,
        services,
    )
    current = dispatch(
        "GET",
        "/admin/tenants/t2/domains/cold_chain_excursion/config",
        admin,
        None,
        services,
    )

    assert forbidden[0] == 403
    assert saved == (200, {"saved": "draft"})
    assert published[0] == 200
    assert published[1]["config_version"] == 1
    assert current[1]["product_profiles"][0]["excursion_allowance_min"] == 30
    assert services.audits.events[-1]["operation"] == "publish"
    assert services.audits.events[-1]["tenant_id"] == "t2"
    incident = services.incidents.get("t2", services.opened_id)
    assert incident.config_snapshot["version"] == 1
    assert incident.config_snapshot["excursion_allowance_min"] == 45


@pytest.mark.security
def test_other_tenant_admin_cannot_edit_contacts() -> None:
    services = _services()
    admin = Principal("t1", "tenant_admin", "admin-1", True)

    response = dispatch(
        "PUT",
        "/admin/tenants/t2/contacts",
        admin,
        {"contacts": [{"contact_id": "C-1", "role": "driver", "e164": "+910000000201"}]},
        services,
    )

    assert response[0] == 403
    assert services.catalog.documents == {}


def test_cadence_editor_rejects_an_incomplete_policy() -> None:
    services = _services()
    admin = Principal("t2", "tenant_admin", "admin-1", True)

    rejected = dispatch(
        "PUT",
        "/admin/tenants/t2/domains/cold_chain_excursion/cadence",
        admin,
        {"policies": []},
        services,
    )
    saved = dispatch(
        "PUT",
        "/admin/tenants/t2/domains/cold_chain_excursion/cadence",
        admin,
        _cadence(),
        services,
    )

    assert rejected[0] == 400
    assert saved == (200, {"saved": "cadence"})


def _services() -> DashboardServices:
    repository = DynamoConfigRepository(FakeDynamoClient(), "tenant")
    repository.register_tenant("t2", ["cold_chain_excursion"])
    incidents = IncidentRepository()
    opened = incidents.create(
        tenant_id="t2",
        shipment_id="VX-3101",
        reason="excursion",
        config_version=1,
        config_snapshot={"version": 1, "excursion_allowance_min": 45},
    )
    services = DashboardServices(
        queries=MemoryIncidentQuery(
            [
                {
                    "tenant_id": "t1",
                    "incident_id": "INC-1",
                    "state": "ASSESSING",
                    "stage": "ASSESSING",
                    "shipment_id": "PH-2841",
                    "allowance_remaining": 41.9,
                    "timeline": ["opened"],
                    "options": [{"id": "FACILITY_A"}],
                    "recommendation": "FACILITY_A",
                    "approval": None,
                    "evidence": {"fault_code": "E17"},
                    "engagement_status": "WAITING",
                },
                {
                    "tenant_id": "t2",
                    "incident_id": "INC-2",
                    "state": "OPEN",
                    "shipment_id": "VX-3101",
                },
            ]
        ),
        repository=repository,
        publisher=ConfigPublisher(repository, clock=lambda: CLOCK),
        catalog=MemoryCatalog(),
        queue=MemoryActionQueue(),
        audits=MemoryAuditLog(),
        clock=lambda: CLOCK,
    )
    services.incidents = incidents
    services.opened_id = opened.incident_id
    return services


def _profiles(allowance: float) -> dict:
    return {
        "product_profiles": [
            {
                "id": "PP-2-8-45",
                "name": "Biologic 2-8",
                "range_c": [2, 8],
                "excursion_allowance_min": allowance,
                "allowance_mode": "cumulative",
            }
        ]
    }


def _cadence() -> dict:
    return {
        "policies": [
            {
                "policy_id": "approval_fast",
                "urgency": "normal",
                "respect_quiet_hours": True,
                "exhaust_after": "PT2M",
                "exhaust_action": {"action": "auto_proceed_or_escalate"},
                "rungs": [
                    {
                        "after": "PT0S",
                        "anchor": "engagement_start",
                        "action": "send",
                        "channel": "whatsapp",
                        "message": "ops_approval",
                    }
                ],
            }
        ]
    }
