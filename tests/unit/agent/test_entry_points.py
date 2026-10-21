"""The six frozen entry points: contract, phase guard, and replay at the edge."""

import importlib
import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oerf.platform.agent import runtime
from oerf.platform.agent.context import StepDependencies
from oerf.platform.incidents.dynamo import DynamoIncidentStore
from oerf.platform.incidents.repository import IncidentNotFound
from oerf.platform.tenancy.config_resolver import get_config
from tests.unit.support.fake_dynamo import FakeDynamoClient

TENANT = "t1"
OTHER_TENANT = "t2"
SHIPMENT = "PH-2841"
TABLE = "incident"
NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
INTERFACES = Path(__file__).resolve().parents[3] / "docs" / "interfaces.md"
ROW = re.compile(r"^\| `(domains\.cold_chain_excursion\.steps\.\w+)` \| `(\w+)` \|", re.MULTILINE)


def _rows() -> dict[str, str]:
    return dict(ROW.findall(INTERFACES.read_text(encoding="utf-8")))


def test_interfaces_freezes_exactly_the_six_phase_modules() -> None:
    rows = _rows()

    assert set(rows) == set(runtime.MODULES.values())
    assert set(rows.values()) == {"handler"}


def test_every_frozen_row_exposes_handler_event_context() -> None:
    for module_path, function in _rows().items():
        module = importlib.import_module(module_path)
        entry = getattr(module, function)

        assert list(inspect.signature(entry).parameters) == ["event", "context"], module_path
        assert module.PHASE == module_path.rsplit(".", 1)[1]


@pytest.mark.parametrize("phase", sorted(runtime.PHASES))
def test_an_entry_point_refuses_a_step_it_does_not_own(phase: str) -> None:
    module = importlib.import_module(runtime.MODULES[phase])
    foreign = next(step for name, step in runtime.PHASES.items() if name != phase)

    with pytest.raises(runtime.StepNotRouted) as error:
        module.handler(
            {
                "step_name": foreign,
                "tenant_id": TENANT,
                "incident_id": "INC-000001",
                "shipment_id": SHIPMENT,
            },
            None,
        )

    assert runtime.module_for(foreign) in str(error.value)


class _UnavailableTableClient(FakeDynamoClient):
    """A client whose transactions fail for a reason a retry cannot fix."""

    def __init__(self) -> None:
        super().__init__()
        self.transactions_enabled = True

    def transact_write_items(self, *, TransactItems: list[Any]) -> dict[str, Any]:
        if not self.transactions_enabled:
            raise RuntimeError("TransactWriteItems is not available in this region")
        return super().transact_write_items(TransactItems=TransactItems)


def _seeded(
    tenant_id: str = TENANT,
    client: FakeDynamoClient | None = None,
) -> tuple[FakeDynamoClient, StepDependencies, str]:
    client = client if client is not None else FakeDynamoClient()
    store = DynamoIncidentStore(client, TABLE, clock=lambda: NOW)
    config = get_config(TENANT, "cold_chain_excursion")
    incident = store.create(
        tenant_id=tenant_id,
        shipment_id=SHIPMENT,
        reason="TEMPERATURE_ABOVE_MAX",
        config_version=config.version,
        config_snapshot=config.model_dump(),
    )
    dependencies = runtime.build_dependencies(
        client=client,
        settings=runtime.RuntimeSettings(incident_table=TABLE),
        clock=lambda: NOW,
    )
    return client, dependencies, incident.incident_id


def _gather_context(tenant_id: str, incident_id: str) -> dict[str, Any]:
    """The payload the definition produces for a ``GatherContext`` task."""
    return {
        "step_name": "GatherContext",
        "workflow": {
            "step": "GatherContext",
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "shipment_id": SHIPMENT,
            "workflow": {
                "trigger": {"temperature_c": 9.2},
                "shipment": {"product_profile_id": "PP-2-8-45"},
                "facilities": [{"id": "facility-1", "drive_min": 12}],
            },
        },
    }


def test_gather_context_moves_the_incident_once_and_replays_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domains.cold_chain_excursion.steps.context import handler

    client, dependencies, incident_id = _seeded()
    monkeypatch.setattr(runtime, "build_dependencies", lambda **_: dependencies)
    event = _gather_context(TENANT, incident_id)

    first = handler(event, SimpleNamespace(aws_request_id="req-1"))
    writes = client.writes
    retry = handler(event, SimpleNamespace(aws_request_id="req-2"))

    store = DynamoIncidentStore(client, TABLE, clock=lambda: NOW)
    assert first["step"] == "GatherContext"
    assert first["last_completed_step"] == "GatherContext"
    assert first["incident_state"] == "DIAGNOSING"
    assert first["workflow"]["context"]["range_c"] == [2.0, 8.0]
    assert "task_token" not in first
    assert [event.state for event in store.events(TENANT, incident_id)] == [
        "OPEN",
        "DIAGNOSING",
    ]
    assert retry == first
    assert client.writes == writes
    assert store.get(TENANT, incident_id).state == "DIAGNOSING"


def test_the_entry_point_builds_its_ports_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domains.cold_chain_excursion.steps.context import handler

    client, _, incident_id = _seeded()
    monkeypatch.setenv("INCIDENT_TABLE_NAME", TABLE)
    monkeypatch.delenv("TENANT_TABLE_NAME", raising=False)
    monkeypatch.setattr(runtime, "_client", None)
    monkeypatch.setattr(runtime.boto3, "client", lambda *args, **kwargs: client)

    response = handler(_gather_context(TENANT, incident_id), None)

    assert response["last_completed_step"] == "GatherContext"
    assert runtime._client is client


def test_the_entry_point_keeps_the_envelope_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domains.cold_chain_excursion.steps.context import handler

    client, dependencies, incident_id = _seeded(tenant_id=OTHER_TENANT)
    monkeypatch.setattr(runtime, "build_dependencies", lambda **_: dependencies)
    store = DynamoIncidentStore(client, TABLE, clock=lambda: NOW)

    handler(_gather_context(OTHER_TENANT, incident_id), None)

    assert store.get(OTHER_TENANT, incident_id).state == "DIAGNOSING"
    with pytest.raises(IncidentNotFound):
        store.get(TENANT, incident_id)


def test_a_repository_failure_escapes_so_step_functions_can_record_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write leaves no partial transition for the retry to duplicate."""
    from domains.cold_chain_excursion.steps.context import handler

    client = _UnavailableTableClient()
    _, dependencies, incident_id = _seeded(client=client)
    monkeypatch.setattr(runtime, "build_dependencies", lambda **_: dependencies)
    event = _gather_context(TENANT, incident_id)
    client.transactions_enabled = False

    with pytest.raises(RuntimeError, match="TransactWriteItems"):
        handler(event, SimpleNamespace(aws_request_id="req-1"))

    client.transactions_enabled = True
    store = DynamoIncidentStore(client, TABLE, clock=lambda: NOW)
    assert store.get(TENANT, incident_id).state == "OPEN"
    assert [timeline.state for timeline in store.events(TENANT, incident_id)] == ["OPEN"]

    assert handler(event, SimpleNamespace(aws_request_id="req-2"))["incident_state"] == "DIAGNOSING"
    assert [timeline.state for timeline in store.events(TENANT, incident_id)] == [
        "OPEN",
        "DIAGNOSING",
    ]
