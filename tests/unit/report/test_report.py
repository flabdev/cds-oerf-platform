"""Golden HTML, missing sections, reopen versioning, checksum, and privacy."""

import hashlib
import threading
from datetime import UTC, datetime

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from oerf.platform.incidents.dynamo import DynamoIncidentStore
from oerf.platform.report.generator import handler
from oerf.platform.report.model import SECTIONS, DecisionTrace, ReportDocument, ReportOption
from oerf.platform.report.service import publish_report
from oerf.platform.report.store import MemoryReportStore, S3ReportStore
from tests.unit.support.fake_dynamo import FakeDynamoClient

GENERATED = "2026-10-20T10:45:00Z"
_DESERIALIZER = TypeDeserializer()


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
    temperature, states = _sections(html, "temperature_timeline", "state_timeline", "diagnosis")
    assert "PH-9" in html
    assert "2026-10-20T10:16:00Z 8.1 C" in temperature
    assert "DIAGNOSING" not in temperature
    assert "TEMPERATURE_EXCURSION" not in temperature
    assert "OPEN -&gt; DIAGNOSING" in states
    assert "8.1 C" not in states
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


def test_concurrent_saves_keep_both_bodies() -> None:
    bucket = _RacingBucket()
    store = S3ReportStore(bucket, "reports")
    results: list = []

    def publish(body: bytes) -> None:
        results.append(store.save("t1", "INC-1", body, "html", GENERATED))

    first = threading.Thread(target=publish, args=(b"first",))
    second = threading.Thread(target=publish, args=(b"second",))
    first.start()
    second.start()
    first.join()
    second.join()

    stored = {
        bucket.objects["reports/t1/INC-1/v1.html"],
        bucket.objects["reports/t1/INC-1/v2.html"],
    }
    assert stored == {b"first", b"second"}
    assert {item.report_version for item in results} == {1, 2}


def test_a_persisted_ingest_reading_reaches_the_temperature_timeline(monkeypatch) -> None:
    """The production ``READ#`` writer and the report reader agree on the row shape."""

    client = FakeDynamoClient()
    incidents = DynamoIncidentStore(client, "incidents", clock=_clock)
    incident = incidents.create(
        tenant_id="t1",
        shipment_id="PH-9",
        reason="TEMPERATURE_EXCURSION",
    )
    assert incidents.record_reading(
        "t1",
        "PH-9",
        event_id="evt-1",
        timestamp="2026-10-20T10:16:00+00:00",
        temperature_c=8.4,
        sequence=7,
    )
    store = MemoryReportStore()
    monkeypatch.setattr("oerf.platform.report.generator._store", lambda: store)
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr(
        "oerf.platform.report.generator.get_dynamodb",
        lambda: _Resource(_ResourceTable(client, "incidents")),
    )

    result = handler({"tenant_id": "t1", "incident_id": incident.incident_id}, None)
    html = store.objects[result["report_key"]].decode()
    temperature, states = _sections(html, "temperature_timeline", "state_timeline", "diagnosis")

    assert result["success"] is True
    assert "2026-10-20T10:16:00Z 8.4 C" in temperature
    assert "8.4 C" not in states


def test_conditional_s3_conflict_reallocates_without_overwriting() -> None:
    bucket = _ConflictingBucket()
    store = S3ReportStore(bucket, "reports")

    receipt = store.save("t1", "INC-1", b"new report", "html", GENERATED)

    assert bucket.attempts == ["reports/t1/INC-1/v1.html", "reports/t1/INC-1/v2.html"]
    assert bucket.objects["reports/t1/INC-1/v1.html"] == b"competing report"
    assert bucket.objects[receipt.s3_key] == b"new report"
    assert receipt.report_version == 2


def test_report_history_queries_read_every_dynamodb_page(monkeypatch) -> None:
    store = MemoryReportStore()
    table = _PagedIncidentTable(_stored_incident())
    monkeypatch.setattr("oerf.platform.report.generator._store", lambda: store)
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr("oerf.platform.report.generator.get_dynamodb", lambda: table)

    result = handler(
        {"tenant_id": "t1", "incident_id": "INC-9"},
        None,
    )
    html = store.objects[result["report_key"]].decode()
    temperature, states = _sections(html, "temperature_timeline", "state_timeline", "diagnosis")

    assert "2026-10-20T10:17:00Z 8.4 C" in temperature
    assert "EVT#2026-10-20T10:17:00Z#0002 OPEN -&gt; DIAGNOSING" in states
    assert table.continuations >= 2


def test_version_allocation_reads_every_list_page() -> None:
    bucket = _PagedBucket()
    store = S3ReportStore(bucket, "reports")

    receipt = store.save("t1", "INC-1", b"next", "html", GENERATED)

    assert receipt.s3_key == "reports/t1/INC-1/v1002.html"
    assert bucket.objects["reports/t1/INC-1/v1.html"] == b"original"
    assert bucket.objects["reports/t1/INC-1/v1001.html"] == b"page-two"


def _sections(html: str, first: str, second: str, third: str) -> tuple[str, str]:
    left = html.split(f'id="{first}"', 1)[1].split(f'id="{second}"', 1)[0]
    right = html.split(f'id="{second}"', 1)[1].split(f'id="{third}"', 1)[0]
    return left, right


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

    def query(self, **kwargs: object) -> dict:
        if self.item is None:
            return {"Items": []}
        values = kwargs.get("ExpressionAttributeValues")
        prefix = values.get(":prefix") if isinstance(values, dict) else ""
        if prefix == "READ#":
            return {"Items": self.item.get("readings", [])}
        if prefix == "EVT#":
            return {"Items": self.item.get("timeline_rows", [])}
        return {"Items": []}


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
        "readings": [
            {
                "sk": "READ#2026-10-20T10:16:00Z",
                "ts": "2026-10-20T10:16:00Z",
                "temperature_c": 8.1,
            }
        ],
        "timeline_rows": [
            {
                "sk": "EVT#2026-10-20T10:16:00Z#0001",
                "previous_state": "OPEN",
                "state": "DIAGNOSING",
                "reason": "TEMPERATURE_EXCURSION",
            }
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


class _RacingBucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self._guard = threading.Lock()
        self._listed = 0
        self._barrier = threading.Barrier(2)

    def list_objects_v2(self, **_kwargs: object) -> dict:
        with self._guard:
            self._listed += 1
            count = self._listed
            keys = list(self.objects)
        if count <= 2:
            self._barrier.wait(timeout=2)
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}

    def put_object(self, **kwargs: object) -> None:
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        with self._guard:
            if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            if isinstance(body, bytes):
                self.objects[key] = body


class _ConflictingBucket:
    """A competing writer claims the listed version between list and put."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.attempts: list[str] = []

    def list_objects_v2(self, **_kwargs: object) -> dict:
        return {"Contents": [{"Key": key} for key in self.objects], "IsTruncated": False}

    def put_object(self, **kwargs: object) -> None:
        key = str(kwargs["Key"])
        self.attempts.append(key)
        if len(self.attempts) == 1:
            self.objects["reports/t1/INC-1/v1.html"] = b"competing report"
            raise ClientError({"Error": {"Code": "ConditionalRequestConflict"}}, "PutObject")
        assert key not in self.objects
        self.objects[key] = kwargs["Body"]


class _PagedIncidentTable(_Table):
    def __init__(self, item: dict) -> None:
        super().__init__(item)
        self.continuations = 0

    def query(self, **kwargs: object) -> dict:
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]
        page_two = kwargs.get("ExclusiveStartKey") is not None
        if page_two:
            self.continuations += 1
            stamp = "2026-10-20T10:17:00Z"
            if prefix == "READ#":
                reading = {"sk": f"READ#{stamp}#000000000008", "ts": stamp, "temperature_c": 8.4}
                return {"Items": [reading]}
            if prefix == "EVT#":
                transition = {
                    "sk": f"EVT#{stamp}#0002",
                    "previous_state": "OPEN",
                    "state": "DIAGNOSING",
                }
                return {"Items": [transition]}
        response = super().query(**kwargs)
        if prefix in {"READ#", "EVT#"}:
            response["LastEvaluatedKey"] = {"pk": "TENANT#t1#INC#INC-9", "sk": f"{prefix}page-two"}
        return response


class _ResourceTable:
    """A boto3-resource-shaped view over one table of the low-level fake client."""

    def __init__(self, client: FakeDynamoClient, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def get_item(self, Key: dict) -> dict:
        loaded = self._client.get_item(
            TableName=self._table,
            Key={name: {"S": value} for name, value in Key.items()},
        )
        item = loaded.get("Item")
        return {"Item": _plain(item)} if item else {}

    def query(self, **kwargs: object) -> dict:
        expression = kwargs["KeyConditionExpression"]
        values = kwargs["ExpressionAttributeValues"]
        assert isinstance(expression, str) and isinstance(values, dict)
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression=expression,
            ExpressionAttributeValues={name: {"S": str(value)} for name, value in values.items()},
        )
        return {"Items": [_plain(item) for item in response.get("Items", [])]}


class _Resource:
    """The minimal ``boto3.resource("dynamodb")`` surface the report reader uses."""

    def __init__(self, table: _ResourceTable) -> None:
        self._table = table

    def Table(self, _name: str) -> _ResourceTable:
        return self._table


def _plain(item: dict) -> dict:
    """Deserialize one low-level row the way the boto3 resource does."""

    return {name: _DESERIALIZER.deserialize(value) for name, value in item.items()}


class _PagedBucket:
    def __init__(self) -> None:
        self.objects = {
            "reports/t1/INC-1/v1.html": b"original",
            "reports/t1/INC-1/v1001.html": b"page-two",
        }

    def list_objects_v2(self, **kwargs: object) -> dict:
        if kwargs.get("ContinuationToken") == "page-2":
            return {
                "Contents": [{"Key": "reports/t1/INC-1/v1001.html"}],
                "IsTruncated": False,
            }
        page = [{"Key": f"reports/t1/INC-1/v{index}.html"} for index in range(1, 1001)]
        return {"Contents": page, "IsTruncated": True, "NextContinuationToken": "page-2"}

    def put_object(self, **kwargs: object) -> None:
        key = str(kwargs["Key"])
        assert key not in self.objects
        body = kwargs["Body"]
        if isinstance(body, bytes):
            self.objects[key] = body


def _clock() -> datetime:
    return datetime(2026, 10, 20, 10, 16, tzinfo=UTC)


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
