"""The report port the GenerateReport step publishes through."""

import pytest

from oerf.platform.agent.reports import GeneratorReportPublisher, ReportPublishError


def test_generator_publisher_returns_a_successful_receipt(monkeypatch) -> None:
    published: dict = {}

    def fake_handler(event: dict, context: object) -> dict:
        published.update(event)
        return {"success": True, "report_key": "reports/t1/INC-000001/v1.html"}

    monkeypatch.setattr("oerf.platform.report.generator.handler", fake_handler)

    receipt = GeneratorReportPublisher().publish(tenant_id="t1", incident_id="INC-000001")

    assert published == {"tenant_id": "t1", "incident_id": "INC-000001"}
    assert receipt["report_key"].endswith("v1.html")


def test_generator_publisher_raises_on_a_rejected_report(monkeypatch) -> None:
    """A handler that returns success=False must fail the step, not pass quietly."""

    monkeypatch.setattr(
        "oerf.platform.report.generator.handler",
        lambda event, context: {"success": False, "error": "incident_not_found"},
    )

    with pytest.raises(ReportPublishError, match="incident_not_found"):
        GeneratorReportPublisher().publish(tenant_id="t1", incident_id="INC-000001")
