"""Golden HTML, missing sections, reopen versioning, checksum, and privacy."""

import hashlib

import pytest

from oerf.platform.report.generator import handler
from oerf.platform.report.model import SECTIONS, DecisionTrace, ReportDocument, ReportOption
from oerf.platform.report.service import publish_report
from oerf.platform.report.store import MemoryReportStore

GENERATED = "2026-10-20T10:45:00Z"


def test_ph2841_html_contains_the_decision_trace_and_every_section() -> None:
    document = _ph2841()
    store = MemoryReportStore()

    receipt = publish_report(document, store, pdf_backend="none")
    html = store.objects[receipt.s3_key].decode()

    assert receipt.s3_key == "reports/t1/INC-000001/v1.html"
    assert receipt.locked is True
    assert receipt.generated_at == GENERATED
    assert receipt.sections == list(SECTIONS)
    assert receipt.sha256 == hashlib.sha256(html.encode()).hexdigest()
    for section in SECTIONS:
        assert f'id="{section}"' in html
    assert "PH-2841" in html
    assert "used_minutes=3" in html
    assert "temperature_c=8.19" in html
    assert "projected_minutes=20.8" in html
    assert "projected_minutes=35.8" in html
    assert "projected_minutes=50.0" in html
    assert "FACILITY_A" in html
    assert "feasible" in html
    assert "infeasible" in html
    assert "+910000000101" not in html
    assert "driver@example.com" not in html
    assert "task-token" not in html


def test_missing_sections_are_explicit() -> None:
    document = ReportDocument(
        tenant_id="t1",
        incident_id="INC-000002",
        generated_at=GENERATED,
    )
    store = MemoryReportStore()

    receipt = publish_report(document, store, pdf_backend="none")
    html = store.objects[receipt.s3_key].decode()

    assert html.count("Not recorded") >= 8
    assert "<section id=\"diagnosis\">" in html


def test_reopen_writes_the_next_version_and_leaves_the_first_intact() -> None:
    store = MemoryReportStore()
    first = publish_report(_ph2841(), store, pdf_backend="none")
    original = store.objects[first.s3_key]

    second = publish_report(
        _ph2841().model_copy(update={"state": "REOPENED"}),
        store,
        pdf_backend="none",
    )

    assert first.report_version == 1
    assert second.report_version == 2
    assert second.s3_key.endswith("/v2.html")
    assert store.objects[first.s3_key] == original
    assert store.objects[second.s3_key] != original


def test_pdf_backend_uses_the_rendered_bytes(monkeypatch) -> None:
    from oerf.platform.report import service

    monkeypatch.setattr(service, "_render_pdf", lambda _html: b"%PDF-1.4 ph2841")
    store = MemoryReportStore()

    receipt = publish_report(_ph2841(), store, pdf_backend="weasyprint")

    assert receipt.s3_key.endswith(".pdf")
    assert receipt.sha256 == hashlib.sha256(b"%PDF-1.4 ph2841").hexdigest()


def test_pdf_backend_without_a_renderer_still_publishes_html(monkeypatch) -> None:
    from oerf.platform.report import service

    monkeypatch.setattr(service, "_render_pdf", lambda _html: None)
    store = MemoryReportStore()

    receipt = publish_report(_ph2841(), store, pdf_backend="weasyprint")

    assert receipt.s3_key.endswith(".html")


@pytest.mark.security
def test_handler_without_a_tenant_does_not_read_or_publish(monkeypatch) -> None:
    monkeypatch.setattr(
        "oerf.platform.report.generator.get_dynamodb",
        lambda: (_ for _ in ()).throw(AssertionError("dynamodb")),
    )
    monkeypatch.setattr(
        "oerf.platform.report.generator._store",
        lambda: (_ for _ in ()).throw(AssertionError("s3")),
    )
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "incidents")

    result = handler({"incident_id": "INC-9", "qa_email": "qa@example.com"}, None)

    assert result == {"success": False, "error": "tenant_required"}


def test_handler_publishes_a_versioned_html_report(monkeypatch) -> None:
    store = MemoryReportStore()
    table = _Table(_stored_incident())
    monkeypatch.setattr("oerf.platform.report.generator._store", lambda: store)
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr("oerf.platform.report.generator.get_dynamodb", lambda: table)

    result = handler(
        {"tenant_id": "t1", "incident_id": "INC-9", "qa_email": "qa@example.com"},
        None,
    )
    html = store.objects[result["report_key"]].decode()

    assert table.keys == [{"pk": "TENANT#t1#INC#INC-9", "sk": "META"}]
    assert result["success"] is True
    assert result["report_key"] == "reports/t1/INC-9/v1.html"
    assert result["locked"] is True
    assert result["sha256"] == hashlib.sha256(html.encode()).hexdigest()
    assert "PH-9" in html
    assert "EVT#2026-10-20T10:16:00Z#0001 OPEN" in html
    assert "used_minutes=3" in html
    assert "qa@example.com" not in html
    assert "+910000000101" not in html
    assert "task-token" not in html
    assert "&lt;b&gt;" in html


def test_handler_does_not_publish_when_the_incident_is_missing(monkeypatch) -> None:
    store = MemoryReportStore()
    table = _Table(None)
    monkeypatch.setattr("oerf.platform.report.generator._store", lambda: store)
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr("oerf.platform.report.generator.get_dynamodb", lambda: table)

    missing = handler({"tenant_id": "t1", "incident_id": "INC-missing"}, None)
    unnamed = handler({"tenant_id": "t1"}, None)

    assert missing == {"success": False, "error": "incident_not_found"}
    assert unnamed == {"success": False, "error": "incident_id_required"}
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "")
    no_table = handler({"tenant_id": "t1", "incident_id": "INC-9"}, None)
    assert no_table == {"success": False, "error": "incident_table_required"}
    assert store.objects == {}


def test_the_same_stored_incident_renders_the_same_html(monkeypatch) -> None:
    store = MemoryReportStore()
    table = _Table(_stored_incident())
    monkeypatch.setattr("oerf.platform.report.generator._store", lambda: store)
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr("oerf.platform.report.generator.get_dynamodb", lambda: table)

    first = handler({"tenant_id": "t1", "incident_id": "INC-9"}, None)
    second = handler({"tenant_id": "t1", "incident_id": "INC-9"}, None)

    assert first["report_version"] == 1
    assert second["report_version"] == 2
    assert store.objects[first["report_key"]] == store.objects[second["report_key"]]


class _Table:
    def __init__(self, item: dict | None) -> None:
        self.item = item
        self.keys: list[dict] = []

    def Table(self, _name: str) -> "_Table":
        return self

    def get_item(self, Key: dict) -> dict:
        self.keys.append(Key)
        if self.item is None:
            return {}
        if Key == {"pk": self.item["pk"], "sk": "META"}:
            return {"Item": self.item}
        return {}

    def query(self, **_kwargs: object) -> dict:
        if self.item is None:
            return {"Items": []}
        return {"Items": self.item.get("timeline_rows", [])}


def _stored_incident() -> dict:
    return {
        "pk": "TENANT#t1#INC#INC-9",
        "sk": "META",
        "tenant_id": "t1",
        "incident_id": "INC-9",
        "shipment_id": "PH-9 <b>",
        "state": "CONTAINED",
        "reason": "temperature",
        "evidence": {"fault_code": "E17", "phone": "+910000000101", "task_token": "task-token"},
        "timeline_rows": [
            {"sk": "EVT#2026-10-20T10:16:00Z#0001", "state": "OPEN", "reason": ""},
        ],
        "decision_trace": {
            "used_minutes": 3,
            "temperature_c": 8.19,
            "allowance_minutes": 45,
            "upper_c": 8,
            "unload_min": 4,
            "rise_rate_c_per_min": 0.03,
            "cooldown_rate_c_per_min": 0.12,
            "min_spare_allowance_min": 5,
            "options": [],
            "recommended": "FACILITY_A",
        },
    }


def _ph2841() -> ReportDocument:
    options = [
        ReportOption(
            id="FACILITY_A",
            projected_minutes=20.8,
            spare_minutes=24.2,
            feasible=True,
            drive_min=9,
        ),
        ReportOption(
            id="FACILITY_B",
            projected_minutes=35.8,
            spare_minutes=9.2,
            feasible=True,
            drive_min=21,
        ),
        ReportOption(
            id="DESTINATION",
            projected_minutes=50.0,
            spare_minutes=-5.0,
            feasible=False,
            drive_min=43,
        ),
    ]
    return ReportDocument(
        tenant_id="t1",
        incident_id="INC-000001",
        shipment_id="PH-2841",
        state="CONTAINED",
        generated_at=GENERATED,
        temperature_timeline=["10:16 8.1 C", "10:33 8.6 C"],
        evidence={
            "fault_code": "E17",
            "phone": "+910000000101",
            "email": "driver@example.com",
            "task_token": "task-token",
        },
        options=options,
        approval="approved",
        facility="FACILITY_A",
        actions=["divert"],
        containment="stable",
        qa_disposition="release",
        decision_trace=DecisionTrace(
            used_minutes=3,
            temperature_c=8.19,
            allowance_minutes=45,
            upper_c=8,
            unload_min=4,
            rise_rate_c_per_min=0.03,
            cooldown_rate_c_per_min=0.12,
            min_spare_allowance_min=5,
            options=options,
            recommended="FACILITY_A",
        ),
    )
