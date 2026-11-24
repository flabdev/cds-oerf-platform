"""Facility catalog decoding from low-level DynamoDB items."""

from oerf.platform.tenancy.facilities import DynamoFacilityCatalog
from tests.unit.support.fake_dynamo import FakeDynamoClient

TABLE = "oerf-tenant"


def _facility_item(
    *,
    facility_id: str,
    emergency_fee_usd: str,
) -> dict:
    """One FACILITY# row in the attribute-value form ``query`` returns."""
    return {
        "pk": {"S": "TENANT#t1"},
        "sk": {"S": f"FACILITY#{facility_id}"},
        "tenant_id": {"S": "t1"},
        "facility_id": {"S": facility_id},
        "name": {"S": f"Facility {facility_id}"},
        "drive_min": {"N": "9" if facility_id == "FAC-A" else "21"},
        "capacity_available": {"BOOL": True},
        "approved": {"BOOL": True},
        "emergency_fee_usd": {"N": emergency_fee_usd},
        "contact_id": {"S": f"C-T1-{facility_id}"},
    }


def test_list_facilities_decodes_emergency_fee_usd_from_dynamodb_items() -> None:
    client = FakeDynamoClient()
    for facility_id, fee in (("FAC-A", "250"), ("FAC-B", "300")):
        client.put_item(
            TableName=TABLE,
            Item=_facility_item(facility_id=facility_id, emergency_fee_usd=fee),
        )

    by_id = {
        row["facility_id"]: row
        for row in DynamoFacilityCatalog(client, TABLE).list_facilities("t1")
    }

    assert by_id["FAC-A"]["approved"] is True
    assert by_id["FAC-A"]["emergency_fee_usd"] == 250.0
    assert by_id["FAC-B"]["emergency_fee_usd"] == 300.0


def test_diagnose_maps_decoded_facility_rows_into_context_candidates() -> None:
    from oerf.platform.agent.context import StepDependencies
    from oerf.platform.agent.envelope import StepEnvelope
    from oerf.platform.agent.handlers import HANDLERS
    from oerf.platform.agent.idempotency import MemoryStepResultStore
    from oerf.platform.incidents.repository import IncidentRepository
    from oerf.platform.tenancy.config_resolver import get_config
    from tests.unit.agent.test_handlers import NOW, SHIPMENT, TENANT

    client = FakeDynamoClient()
    client.put_item(
        TableName=TABLE,
        Item=_facility_item(facility_id="FAC-A", emergency_fee_usd="250"),
    )

    catalog = DynamoFacilityCatalog(client, TABLE)
    incidents = IncidentRepository(clock=lambda: NOW)
    config = get_config(TENANT, "cold_chain_excursion")
    incident = incidents.create(
        tenant_id=TENANT,
        shipment_id=SHIPMENT,
        reason="temperature excursion",
        config_version=config.version,
        config_snapshot=config.model_dump(),
    )
    incidents.transition(TENANT, incident.incident_id, from_state="OPEN", to_state="DIAGNOSING")
    dependencies = StepDependencies(
        incidents=incidents,
        results=MemoryStepResultStore(clock=lambda: NOW),
        facilities=catalog,
        clock=lambda: NOW,
    )
    envelope = StepEnvelope(
        step="Diagnose",
        tenant_id=TENANT,
        incident_id=incident.incident_id,
        shipment_id=SHIPMENT,
        workflow={
            "context": {
                "range_c": [2, 8],
                "allowance_minutes": 45,
                "trigger": {"temperature_c": 8.5, "timestamp": "2026-10-20T10:10:00Z"},
            },
            "trigger": {"timestamp": "2026-10-20T10:10:00Z"},
        },
    )

    result = HANDLERS["Diagnose"](envelope, dependencies)
    facility_a = next(
        row for row in result.workflow["context"]["facilities"] if row["facility_id"] == "FAC-A"
    )

    assert facility_a["approved"] is True
    assert facility_a["emergency_fee_usd"] == 250.0