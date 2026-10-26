"""The step container: phase routing, port wiring, and dependency injection."""

from datetime import UTC, datetime

import pytest

from domains.cold_chain_excursion.steps import (
    act,
    assess,
    cancel,
    close,
    context,
    diagnose,
    escalate,
    qa,
    record_failure,
    verify,
)
from oerf.platform.agent import runtime
from oerf.platform.agent.dynamo_step_results import DynamoStepResultStore
from oerf.platform.agent.handlers import HANDLERS
from oerf.platform.incidents.dynamo import DynamoIncidentStore
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from tests.unit.support.fake_dynamo import FakeDynamoClient

TENANT = "t1"
SHIPMENT = "PH-2841"
NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
STEP_MODULES = (
    context,
    diagnose,
    assess,
    act,
    verify,
    qa,
    close,
    record_failure,
    escalate,
    cancel,
)


def test_phase_map_covers_the_frozen_modules() -> None:
    assert runtime.PHASES == {
        "context": "GatherContext",
        "diagnose": "Diagnose",
        "assess": "Assess",
        "act": "Act",
        "verify": "Verify",
        "qa": "QA",
        "close": "Close",
        "record_failure": "RecordFailure",
        "escalate": "Escalate",
        "cancel": "Cancel",
    }
    assert set(runtime.MODULES.values()) == {module.__name__ for module in STEP_MODULES}
    assert set(runtime.PHASES.values()) <= set(HANDLERS)


def test_phase_lookup_names_the_module_that_owns_a_step() -> None:
    assert runtime.phase_for("QA") == "qa"
    assert runtime.module_for("QA") == "domains.cold_chain_excursion.steps.qa"
    # Steps OERF-203, OERF-206 and OERF-207 add have no entry point yet.
    assert runtime.phase_for("GenerateReport") is None
    assert runtime.module_for("GenerateReport") is None


def test_settings_are_read_from_the_lambda_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INCIDENT_TABLE_NAME", "incident")
    monkeypatch.setenv("TENANT_TABLE_NAME", "tenant")

    settings = runtime.RuntimeSettings.from_environment()

    assert settings.incident_table == "incident"
    assert settings.tenant_table == "tenant"


def test_a_missing_incident_table_fails_before_an_adapter_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INCIDENT_TABLE_NAME", raising=False)

    with pytest.raises(runtime.ConfigurationError):
        runtime.RuntimeSettings.from_environment()
    with pytest.raises(runtime.ConfigurationError):
        runtime.RuntimeSettings(incident_table="  ")


def test_container_wires_every_port_over_one_client_and_clock() -> None:
    client = FakeDynamoClient()
    settings = runtime.RuntimeSettings(incident_table="incident", tenant_table="tenant")

    dependencies = runtime.build_dependencies(client=client, settings=settings, clock=lambda: NOW)

    assert isinstance(dependencies.incidents, DynamoIncidentStore)
    assert isinstance(dependencies.results, DynamoStepResultStore)
    assert isinstance(dependencies.config, DynamoConfigRepository)
    from oerf.platform.tenancy.facilities import DynamoFacilityCatalog

    assert isinstance(dependencies.facilities, DynamoFacilityCatalog)
    assert dependencies.now() == NOW


def test_config_port_is_optional_without_a_tenant_table() -> None:
    dependencies = runtime.build_dependencies(
        client=FakeDynamoClient(),
        settings=runtime.RuntimeSettings(incident_table="incident"),
    )

    assert dependencies.config is None
    assert dependencies.facilities is None


def test_an_unknown_phase_is_rejected_before_the_event_is_read() -> None:
    with pytest.raises(runtime.UnknownPhase):
        runtime.run_step("report", {})


def test_a_foreign_step_is_rejected_before_any_client_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> FakeDynamoClient:
        raise AssertionError("routing must fail before the container builds a client")

    monkeypatch.setattr(runtime, "dynamodb_client", forbidden)

    with pytest.raises(runtime.StepNotRouted) as error:
        context.handler(
            {
                "step_name": "Assess",
                "tenant_id": TENANT,
                "incident_id": "INC-000001",
                "shipment_id": SHIPMENT,
            },
            None,
        )

    assert "domains.cold_chain_excursion.steps.assess" in str(error.value)
    assert "GatherContext" in str(error.value)
