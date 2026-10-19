"""Golden HTML, missing sections, reopen versioning, checksum, and privacy."""

import hashlib

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


def test_handler_publishes_a_versioned_html_report(monkeypatch) -> None:
    store = MemoryReportStore()
    monkeypatch.setattr("oerf.platform.report.generator._store", lambda: store)
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "")

    result = handler(
        {"tenant_id": "t1", "incident_id": "INC-9", "qa_email": "qa@example.com"},
        None,
    )

    assert result["success"] is True
    assert result["report_key"] == "reports/t1/INC-9/v1.html"
    assert result["locked"] is True
    assert result["sha256"]
    assert "qa@example.com" not in store.objects[result["report_key"]].decode()


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
