"""Tests for the cold-chain domain files and the T1 seed."""

import json

from jsonschema import Draft202012Validator

from oerf.platform.tenancy.config_resolver import find_repo_root, get_config
from sim.seed.load import load_seed

CATALOG_IDS = {
    "driver_checkin",
    "driver_reminder",
    "driver_call",
    "supervisor_escalation",
    "ops_approval",
    "approval_reminder",
    "facility_request",
    "facility_reminder",
    "driver_reroute",
    "driver_ack",
    "transfer_confirm_request",
    "qa_report_ready",
    "qa_reminder",
    "final_question",
    "evidence_confirm",
    "evidence_unreadable",
    "concern_reply",
    "evidence_resubmitted",
    "status_update",
    "disambiguation",
    "auto_proceed_notice",
    "voice_note_unsupported",
    "ops_escalation_options",
    "qa_prewarn",
    "reopen_alert",
    "callback_request",
    "callback_ack",
    "transfer_escalation",
    "reroute_ack_escalation",
    "incident_cancelled",
    "judge_welcome",
    "sandbox_full",
    "reopened_late_reply",
}


def test_defaults_match_the_domain_schema() -> None:
    domain_dir = find_repo_root() / "domains" / "cold_chain_excursion"
    schema = json.loads((domain_dir / "schema.json").read_text(encoding="utf-8"))
    defaults = json.loads((domain_dir / "defaults.json").read_text(encoding="utf-8"))

    errors = list(Draft202012Validator(schema).iter_errors(defaults))

    assert errors == []
    assert defaults["domain"] == "cold_chain_excursion"
    assert defaults["product_profiles"][0]["range_c"] == [2, 8]
    assert defaults["product_profiles"][0]["excursion_allowance_min"] == 45


def test_message_catalog_uses_only_specified_template_text() -> None:
    path = find_repo_root() / "domains" / "cold_chain_excursion" / "messages.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))

    assert catalog["domain"] == "cold_chain_excursion"
    assert set(catalog["messages"]) == CATALOG_IDS
    assert catalog["messages"]["driver_checkin"]["sms"] == (
        "Temperature alert {shipment_id}. Pull over safely and check WhatsApp."
    )


def test_t1_seed_contains_shipment_roles_and_facilities() -> None:
    seed = load_seed("t1")

    assert seed["tenant"]["tenant_id"] == "t1"
    assert seed["tenant"]["PK"] == "TENANT#t1"
    shipment = seed["shipments"][0]
    assert shipment["shipment_id"] == "PH-2841"
    assert shipment["product_profile"] == "PP-2-8-45"
    assert shipment["eta_min_at_excursion"] == 46
    roles = {contact["party_role"] for contact in seed["contacts"]}
    assert {"driver", "ops_manager", "qa", "facility"} <= roles
    facilities = {facility["facility_id"] for facility in seed["facilities"]}
    assert facilities == {"FAC-A", "FAC-B"}
    facility_a = next(item for item in seed["facilities"] if item["facility_id"] == "FAC-A")
    facility_b = next(item for item in seed["facilities"] if item["facility_id"] == "FAC-B")
    assert facility_a["drive_min"] == 9
    assert facility_a["emergency_fee_usd"] == 250
    assert facility_b["drive_min"] == 21
    assert facility_b["distance_km"] == 14
    assert facility_b["emergency_fee_usd"] == 300
    _assert_tenant_owned(seed)


def test_seed_load_is_repeatable() -> None:
    assert load_seed("t1") == load_seed("t1")


def test_resolved_t1_config_still_validates() -> None:
    config = get_config(tenant_id="t1", domain="cold_chain_excursion")

    assert config.product_profile("PP-2-8-45").excursion_allowance_min == 45


def _assert_tenant_owned(seed: dict[str, object]) -> None:
    for name, document in seed.items():
        records = document if isinstance(document, list) else [document]
        for record in records:
            assert isinstance(record, dict)
            assert record["tenant_id"] == "t1"
            assert str(record["PK"]).startswith("TENANT#t1")
