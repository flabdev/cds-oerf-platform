"""T2 and sandbox seeds stay on the 45-minute allowance."""

import json

from oerf.platform.tenancy.config_resolver import find_repo_root, get_config
from sim.seed.load import load_seed


def test_t2_keeps_the_seed_allowance_and_changes_identity() -> None:
    seed = load_seed("t2")
    config = get_config(tenant_id="t2", domain="cold_chain_excursion")

    assert seed["tenant"]["tenant_id"] == "t2"
    assert seed["tenant"]["brand_name"] == "Sample Vaccine 3PL"
    assert seed["tenant"]["demo"] is True
    assert seed["shipments"][0]["shipment_id"] == "VX-3101"
    assert seed["shipments"][0]["device"] == "RF-7782"
    assert seed["shipments"][0]["eta_min_at_excursion"] == 46
    assert {item["facility_id"] for item in seed["facilities"]} == {"FAC-A", "FAC-B"}
    assert seed["facilities"][0]["drive_min"] == 9
    assert all(contact["scripted"] is True for contact in seed["contacts"])
    assert len(seed["contacts"]) == 8
    assert config.product_profile("PP-2-8-45").excursion_allowance_min == 45
    assert seed["cadence/approval_fast"]["exhaust_after"] == "PT1M"
    _assert_owned(seed, "t2")


def test_sbx_is_a_demo_tenant_with_one_seed_shipment() -> None:
    seed = load_seed("sbx")
    config = get_config(tenant_id="sbx", domain="cold_chain_excursion")

    assert seed["tenant"]["brand_name"] == "OERF Sandbox"
    assert seed["tenant"]["demo"] is True
    assert seed["tenant"]["clock_multiplier"] == 20
    assert seed["shipments"][0]["shipment_id"] == "SBX-0001"
    assert seed["shipments"][0]["device"] == "RF-SBX-0001"
    assert all(contact["scripted"] is True for contact in seed["contacts"])
    assert config.product_profile("PP-2-8-45").excursion_allowance_min == 45
    _assert_owned(seed, "sbx")


def test_photo_labels_are_synthetic_and_render(tmp_path) -> None:
    import importlib.util

    folder = find_repo_root() / "tests" / "ai-evals" / "photos"
    raw = (folder / "labels.json").read_text(encoding="utf-8")
    labels = json.loads(raw)
    blob = raw.lower()

    assert len(labels) == 18
    assert "gmail" not in blob
    assert "+91" not in blob
    assert sum(1 for item in labels if item["ambiguity"] == "unreadable") == 3
    assert sum(1 for item in labels if item["wrong_unit"]) == 2
    high = [item for item in labels if item["confidence_band"] == "high" and item["expected_code"]]
    assert {item["expected_code"] for item in high} >= {"E17", "E05", "E22"}

    spec = importlib.util.spec_from_file_location("photo_render", folder / "render.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "labels.json").write_text(raw, encoding="utf-8")
    written = module.render_photos(tmp_path)
    assert written[0].read_bytes().startswith(b"\x89PNG")


def _assert_owned(seed: dict[str, object], tenant_id: str) -> None:
    for document in seed.values():
        records = document if isinstance(document, list) else [document]
        for record in records:
            assert isinstance(record, dict)
            assert record["tenant_id"] == tenant_id
            assert str(record["PK"]).startswith(f"TENANT#{tenant_id}")
