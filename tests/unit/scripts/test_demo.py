"""Operator demo commands. Stubber stands in for AWS. Nothing here calls the network."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber

from oerf.platform.storage.dynamo import incident_pk, key
from scripts.demo import (
    DemoError,
    _list_executions as list_executions,
    child_line,
    cleanup,
    incident_from_name,
    main,
    parse_temps,
    path_line,
    post_driver_reply,
    prepare,
    public_error,
    publish_excursion,
    reading_note,
    rehearse,
    resolve_subject,
    show_status,
)
from scripts.demo_reply import ROUTER_FUNCTION
from scripts.seed_runtime import seed_runtime
from tests.unit.support.fake_dynamo import FakeDynamoClient

NOW = datetime(2026, 10, 28, 15, 13, 5, tzinfo=UTC)
TENANT_TABLE = "tenant"
ROUTING_TABLE = "routing"
INCIDENT_TABLE = "incident"
ACCOUNT = "000000000000"
OTHER_ACCOUNT = "111111111111"
COLD_ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:oerf-cold-chain"
ENGAGE_ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:oerf-engage-party"
ROLE_ARN = "arn:aws:iam::000000000000:role/hidden"
PHONE = "+15550100001"
SENTINEL = "PAYLOAD-SENTINEL"
EVENT_ID = "event-should-not-print"


class Timeline:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Session:
    def __init__(self, **clients: object) -> None:
        self.clients = clients
        self.asked: list[str] = []

    def client(self, name: str, region_name: str | None = None) -> object:
        assert region_name == "us-east-1"
        self.asked.append(name)
        if name not in self.clients:
            raise AssertionError(f"unexpected client {name}")
        return self.clients[name]


class Capture:
    def __init__(self, client: object) -> None:
        self.client = client
        self.calls: list[dict] = []

    def put_events(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return self.client.put_events(**kwargs)


def test_seed_subject_follows_the_tenant() -> None:
    assert resolve_subject("t1", None, None) == ("PH-2841", "RF-7781")
    assert resolve_subject("t2", None, None) == ("VX-3101", "RF-7782")
    assert resolve_subject("t1", "CUSTOM", "DEV-1") == ("CUSTOM", "DEV-1")


def test_temps_need_two_finite_numbers() -> None:
    assert parse_temps("8.1,8.6") == (8.1, 8.6)
    with pytest.raises(DemoError, match="at least two"):
        parse_temps("8.1")


def test_all_tenants_are_refused_before_aws() -> None:
    def factory(_profile: str) -> Session:
        raise AssertionError("aws")

    with pytest.raises(SystemExit, match="refused"):
        main(
            ["preflight", "--profile", "demo", "--account", ACCOUNT, "--tenant", "all"],
            session_factory=factory,
        )


def test_a_wrong_account_stops_before_any_other_call(capsys: pytest.CaptureFixture[str]) -> None:
    sts, stubber = _aws("sts")
    stubber.add_response("get_caller_identity", _identity(OTHER_ACCOUNT))
    session = Session(sts=sts)

    code = main(
        ["sensor", "--profile", "demo", "--account", ACCOUNT, "--tenant", "t1"],
        session_factory=lambda _p: session,
    )

    assert code == 1
    assert session.asked == ["sts"]
    out, err = capsys.readouterr()
    assert "refusing account" in err
    assert ACCOUNT not in out + err
    assert OTHER_ACCOUNT not in out + err
    assert "arn:aws" not in out + err
    stubber.assert_no_pending_responses()


def test_preflight_passes_when_the_account_is_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, stubbers = _ready_session(seeded=True)

    code = main(
        ["preflight", "--profile", "demo", "--account", ACCOUNT, "--tenant", "t1"],
        session_factory=lambda _p: session,
    )

    out, err = capsys.readouterr()
    assert code == 0
    assert err == ""
    assert "PASS account" in out
    for name in ("OerfDataStack", "OerfComputeStack", "OerfApiStack", "OerfObservabilityStack"):
        assert f"PASS stack {name}" in out
    assert "PASS state machine oerf-cold-chain" in out
    assert "PASS state machine oerf-engage-party" in out
    assert "PASS runtime rows" in out
    assert "arn:aws" not in out
    assert PHONE not in out
    assert ACCOUNT not in out
    for stubber in stubbers:
        stubber.assert_no_pending_responses()


def test_preflight_fails_a_stack_that_is_still_updating(capsys: pytest.CaptureFixture[str]) -> None:
    session, stubbers = _ready_session(seeded=True, data_status="UPDATE_IN_PROGRESS")

    code = main(
        ["preflight", "--profile", "demo", "--account", ACCOUNT, "--tenant", "t1"],
        session_factory=lambda _p: session,
    )

    out, _err = capsys.readouterr()
    assert code == 1
    assert "FAIL stack OerfDataStack UPDATE_IN_PROGRESS" in out
    assert "arn:aws" not in out
    for stubber in stubbers:
        stubber.assert_no_pending_responses()


def test_preflight_fails_when_runtime_rows_would_change(capsys: pytest.CaptureFixture[str]) -> None:
    session, stubbers = _ready_session(seeded=False)

    code = main(
        ["preflight", "--profile", "demo", "--account", ACCOUNT, "--tenant", "t1"],
        session_factory=lambda _p: session,
    )

    out, _err = capsys.readouterr()
    assert code == 1
    assert "would change" in out
    assert "PASS runtime rows" not in out
    assert PHONE not in out
    for stubber in stubbers:
        stubber.assert_no_pending_responses()


def test_preflight_live_checks_the_driver_parameter_without_printing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, stubbers = _ready_session(seeded=True)
    ssm, ssm_stub = _aws("ssm")
    ssm_stub.add_response(
        "get_parameter",
        {
            "Parameter": {
                "Name": "/oerf/demo-numbers/t1/driver",
                "Value": PHONE,
                "Type": "SecureString",
            }
        },
        {"Name": "/oerf/demo-numbers/t1/driver", "WithDecryption": True},
    )
    session.clients["ssm"] = ssm

    code = main(
        ["preflight", "--profile", "demo", "--account", ACCOUNT, "--tenant", "t1", "--live"],
        session_factory=lambda _p: session,
    )

    out, err = capsys.readouterr()
    assert code == 0
    assert "PASS driver parameter" in out
    assert PHONE not in out + err
    ssm_stub.assert_no_pending_responses()
    for stubber in stubbers:
        stubber.assert_no_pending_responses()


def test_preflight_live_fails_when_the_driver_parameter_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, stubbers = _ready_session(seeded=True)
    ssm, ssm_stub = _aws("ssm")
    ssm_stub.add_client_error("get_parameter", service_error_code="ParameterNotFound")
    session.clients["ssm"] = ssm

    code = main(
        ["preflight", "--profile", "demo", "--account", ACCOUNT, "--tenant", "t1", "--live"],
        session_factory=lambda _p: session,
    )

    out, err = capsys.readouterr()
    assert code == 1
    assert "FAIL driver parameter" in out
    assert PHONE not in out + err
    ssm_stub.assert_no_pending_responses()
    for stubber in stubbers:
        stubber.assert_no_pending_responses()


def test_prep_reseeds_then_requires_a_clean_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple] = []

    def reset_fn(_clients: object, tenant: str, *, apply: bool, reseed: bool) -> dict[str, int]:
        calls.append(("reset", tenant, apply, reseed))
        return {"executions": 0}

    def seed_fn(*_args: object, apply: bool, **_kwargs: object) -> dict[str, int]:
        calls.append(("seed", apply))
        return {}

    prepare(_clients(), "t1", reset_fn=reset_fn, seed_fn=seed_fn)

    assert calls == [
        ("reset", "t1", True, True),
        ("seed", True),
        ("reset", "t1", False, True),
    ]
    assert capsys.readouterr().out == "prep t1 ready\n"


def test_prep_fails_when_the_dry_run_is_not_clear() -> None:
    def reset_fn(_clients: object, _tenant: str, *, apply: bool, reseed: bool) -> dict[str, int]:
        return {"executions": 0 if apply else 2, "incident_items": 0}

    with pytest.raises(Exception, match="executions=2"):
        prepare(_clients(), "t1", reset_fn=reset_fn, seed_fn=lambda *_a, **_k: {})


def test_cleanup_resets_then_requires_zero(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[bool, bool]] = []

    def reset_fn(_clients: object, _tenant: str, *, apply: bool, reseed: bool) -> dict[str, int]:
        calls.append((apply, reseed))
        return {"incident_items": 0}

    cleanup(_clients(), "t1", reset_fn=reset_fn)

    assert calls == [(True, False), (False, False)]
    assert capsys.readouterr().out == "cleanup t1 clear\n"


def test_cleanup_fails_when_items_remain() -> None:
    def reset_fn(_clients: object, _tenant: str, *, apply: bool, reseed: bool) -> dict[str, int]:
        return {"incident_items": 0 if apply else 1}

    with pytest.raises(Exception, match="incident_items=1"):
        cleanup(_clients(), "t1", reset_fn=reset_fn)


def test_commands_do_not_shell_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("shell out")

    monkeypatch.setattr("subprocess.run", boom)
    prepare(
        _clients(),
        "t1",
        reset_fn=lambda *_a, **_k: {"executions": 0},
        seed_fn=lambda *_a, **_k: {},
    )


def test_sensor_publishes_the_run_sheet_detail_and_hides_the_event_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dynamo, dynamo_stub = _aws("dynamodb")
    _stub_range(dynamo_stub)
    steps, step_stub = _aws("stepfunctions")
    step_stub.add_response("list_executions", {"executions": []})
    step_stub.add_response(
        "list_executions",
        {"executions": [_execution_item("t1-INC-000012", COLD_ARN)]},
    )
    events, event_stub = _aws("events")
    for _event in range(2):
        event_stub.add_response(
            "put_events",
            {"FailedEntryCount": 0, "Entries": [{"EventId": EVENT_ID}]},
        )
    clock = Timeline()

    incident_id = publish_excursion(
        _clients(dynamodb=dynamo, stepfunctions=steps),
        Capture(events),
        "t1",
        "PH-2841",
        "RF-7781",
        (8.1, 8.6),
        4,
        clock,
        clock.sleep,
    )

    out, err = capsys.readouterr()
    later = (NOW + timedelta(seconds=4)).astimezone().strftime("%H:%M:%S")
    first = NOW.astimezone().strftime("%H:%M:%S")
    assert incident_id == "INC-000012"
    assert err == ""
    assert out.splitlines() == [
        "OERF reefer simulator · tenant t1 · shipment PH-2841 · device RF-7781 · safe range 2–8 °C",
        f"{first}  RF-7781  8.1 °C  above safe range",
        f"{later}  RF-7781  8.6 °C  excursion confirmed → incident opening…",
        f"{later}  Incident INC-000012 open · waiting for the driver on WhatsApp",
    ]
    assert EVENT_ID not in out
    assert "arn:aws" not in out
    assert "{" not in out
    dynamo_stub.assert_no_pending_responses()
    step_stub.assert_no_pending_responses()
    event_stub.assert_no_pending_responses()


def test_sensor_detail_is_thirty_seconds_apart() -> None:
    dynamo, dynamo_stub = _aws("dynamodb")
    _stub_range(dynamo_stub)
    steps, step_stub = _aws("stepfunctions")
    step_stub.add_response("list_executions", {"executions": []})
    step_stub.add_response(
        "list_executions",
        {"executions": [_execution_item("t1-INC-000003", COLD_ARN)]},
    )
    events, event_stub = _aws("events")
    event_stub.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]})
    event_stub.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "2"}]})
    published = Capture(events)
    clock = Timeline()

    publish_excursion(
        _clients(dynamodb=dynamo, stepfunctions=steps),
        published,
        "t1",
        "PH-2841",
        "RF-7781",
        (8.1, 8.6),
        0,
        clock,
        clock.sleep,
    )

    details = [json.loads(call["Entries"][0]["Detail"]) for call in published.calls]
    first = datetime.strptime(details[0]["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    second = datetime.strptime(details[1]["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert second - first == timedelta(seconds=30)
    assert details[0]["seq"] == 1
    assert details[1]["seq"] == 2
    assert details[0]["temperature_c"] == 8.1
    assert details[1]["temperature_c"] == 8.6
    assert details[0]["lat"] == 17.44
    assert details[0]["lon"] == 78.38
    assert details[0]["tenant_id"] == "t1"
    assert details[0]["shipment_id"] == "PH-2841"
    assert details[0]["device_id"] == "RF-7781"
    assert published.calls[0]["Entries"][0]["Source"] == "sim.reefer"
    assert published.calls[0]["Entries"][0]["DetailType"] == "TemperatureReading"
    assert published.calls[0]["Entries"][0]["EventBusName"] == "oerf-events"
    dynamo_stub.assert_no_pending_responses()
    step_stub.assert_no_pending_responses()
    event_stub.assert_no_pending_responses()


def test_sensor_times_out_when_no_execution_opens(capsys: pytest.CaptureFixture[str]) -> None:
    dynamo, dynamo_stub = _aws("dynamodb")
    _stub_range(dynamo_stub)
    steps, step_stub = _aws("stepfunctions")
    for _page in range(4):
        step_stub.add_response("list_executions", {"executions": []})
    events, event_stub = _aws("events")
    event_stub.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]})
    event_stub.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "2"}]})
    clock = Timeline()

    with pytest.raises(Exception, match="no cold-chain execution opened for t1"):
        publish_excursion(
            _clients(dynamodb=dynamo, stepfunctions=steps),
            events,
            "t1",
            "PH-2841",
            "RF-7781",
            (8.1, 8.6),
            0,
            clock,
            clock.sleep,
            timeout=2,
        )

    out = capsys.readouterr().out
    assert "Incident" not in out
    assert "arn:aws" not in out
    dynamo_stub.assert_no_pending_responses()
    step_stub.assert_no_pending_responses()
    event_stub.assert_no_pending_responses()


def test_a_rejected_reading_does_not_print_the_api_body(capsys: pytest.CaptureFixture[str]) -> None:
    dynamo, dynamo_stub = _aws("dynamodb")
    _stub_range(dynamo_stub)
    steps, step_stub = _aws("stepfunctions")
    step_stub.add_response("list_executions", {"executions": []})
    events, event_stub = _aws("events")
    event_stub.add_response(
        "put_events",
        {
            "FailedEntryCount": 1,
            "Entries": [
                {
                    "ErrorCode": "InternalFailure",
                    "ErrorMessage": f"{PHONE} arn:aws:events:hidden",
                }
            ],
        },
    )

    with pytest.raises(Exception, match="not accepted"):
        publish_excursion(
            _clients(dynamodb=dynamo, stepfunctions=steps),
            events,
            "t1",
            "PH-2841",
            "RF-7781",
            (8.1, 8.6),
            0,
            Timeline(),
            lambda _seconds: None,
        )

    out, err = capsys.readouterr()
    assert PHONE not in out + err
    assert "arn:aws" not in out + err
    assert EVENT_ID not in out + err
    dynamo_stub.assert_no_pending_responses()
    step_stub.assert_no_pending_responses()
    event_stub.assert_no_pending_responses()


def test_list_executions_follows_the_page_token() -> None:
    steps, stubber = _aws("stepfunctions")
    stubber.add_response("list_executions", {"executions": [], "nextToken": "more"})
    stubber.add_response(
        "list_executions",
        {"executions": [_execution_item("t1-INC-000012", COLD_ARN)]},
    )

    found = list_executions(steps, COLD_ARN)

    assert [item.name for item in found] == ["t1-INC-000012"]
    stubber.assert_no_pending_responses()


def test_reply_refuses_live_before_any_aws_call(capsys: pytest.CaptureFixture[str]) -> None:
    sts, stubber = _aws("sts")
    stubber.add_response("get_caller_identity", _identity(ACCOUNT))
    session = Session(sts=sts)

    code = main(
        ["reply", "--profile", "demo", "--account", ACCOUNT, "--tenant", "t1", "--live", "--button", "running"],
        session_factory=lambda _p: session,
    )

    _out, err = capsys.readouterr()
    assert code == 1
    assert session.asked == ["sts"]
    assert "mocked" in err
    assert PHONE not in err
    stubber.assert_no_pending_responses()


def test_reply_waits_for_send_via_router_then_posts_the_button() -> None:
    steps, step_stub = _aws("stepfunctions")
    lam, lambda_stub = _aws("lambda")
    lambda_stub.add_response(
        "get_function_configuration",
        _router("true"),
    )
    _stub_child(step_stub, "ComputeNextRung")
    _stub_child(step_stub, "SendViaRouter")
    sent: list[str] = []
    clock = Timeline()

    post_driver_reply(
        _clients(stepfunctions=steps),
        lam,
        "demo",
        "t1",
        "PH-2841",
        "stopped",
        live=False,
        clock=clock,
        sleep=clock.sleep,
        send_reply=lambda _profile, _tenant, _shipment, button: sent.append(button),
        incident_id="INC-000012",
    )

    assert sent == ["stopped"]
    step_stub.assert_no_pending_responses()
    lambda_stub.assert_no_pending_responses()


def test_reply_refuses_a_live_router_without_posting() -> None:
    steps, step_stub = _aws("stepfunctions")
    lam, lambda_stub = _aws("lambda")
    lambda_stub.add_response(
        "get_function_configuration",
        _router("false"),
    )
    sent: list[str] = []

    with pytest.raises(Exception, match="mocked"):
        post_driver_reply(
            _clients(stepfunctions=steps),
            lam,
            "demo",
            "t1",
            "PH-2841",
            "running",
            live=False,
            clock=Timeline(),
            sleep=lambda _seconds: None,
            send_reply=lambda *_args: sent.append("sent"),
            incident_id="INC-000012",
        )

    assert sent == []
    step_stub.assert_no_pending_responses()
    lambda_stub.assert_no_pending_responses()


def test_status_hides_arns_and_payloads(capsys: pytest.CaptureFixture[str]) -> None:
    steps, step_stub = _aws("stepfunctions")
    dynamo, dynamo_stub = _aws("dynamodb")
    names = [
        "Open",
        "GatherContext",
        "Diagnose",
        "EngageDriverDiagnosis",
        "Assess",
        "NeedApproval?",
        "Act",
        "VerifyLoop",
        "Qa",
        "Close",
    ]
    step_stub.add_response(
        "list_executions",
        {"executions": [_execution_item("t1-INC-000012", COLD_ARN)]},
    )
    events = [_entered(index, name) for index, name in enumerate(names, start=1)]
    step_stub.add_response("get_execution_history", {"events": events})
    step_stub.add_response(
        "list_executions",
        {"executions": [_execution_item("child-1", ENGAGE_ARN)]},
    )
    step_stub.add_response(
        "describe_execution",
        _described(
            "child-1",
            ENGAGE_ARN,
            {"tenant_id": "t1", "incident_id": "INC-000012", "sender": PHONE, "sentinel": SENTINEL},
        ),
    )
    step_stub.add_response(
        "get_execution_history",
        {"events": [_entered(1, "SendViaRouter")]},
    )
    dynamo_stub.add_response(
        "get_item",
        {"Item": {**key(incident_pk("t1", "INC-000012"), "META"), "state": {"S": "DIAGNOSING"}}},
        {"TableName": INCIDENT_TABLE, "Key": key(incident_pk("t1", "INC-000012"), "META")},
    )

    show_status(_clients(dynamodb=dynamo, stepfunctions=steps), "t1")

    out = capsys.readouterr().out
    assert out.splitlines() == [
        "parent t1-INC-000012 RUNNING",
        "child RUNNING SendViaRouter",
        "path " + " → ".join(names[-8:]),
        "incident INC-000012 DIAGNOSING",
    ]
    assert "arn:aws" not in out
    assert PHONE not in out
    assert SENTINEL not in out
    step_stub.assert_no_pending_responses()
    dynamo_stub.assert_no_pending_responses()


def test_run_order_skips_the_reply_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr("scripts.demo._resources", lambda _session: object())
    monkeypatch.setattr("scripts.demo.prepare", lambda *_a, **_k: order.append("prep"))
    monkeypatch.setattr(
        "scripts.demo._client",
        lambda _session, name: order.append(f"client:{name}") or object(),
    )
    monkeypatch.setattr("scripts.demo.publish_excursion", lambda *_a, **_k: order.append("sensor"))
    monkeypatch.setattr("scripts.demo.router_is_live", lambda _client: False)
    monkeypatch.setattr("scripts.demo.post_driver_reply", lambda *_a, **_k: order.append("reply"))
    monkeypatch.setattr("scripts.demo.show_status", lambda *_a, **_k: order.append("status"))

    rehearse(
        object(),
        "demo",
        "t1",
        "PH-2841",
        "RF-7781",
        (8.1, 8.6),
        4,
        "running",
        live=False,
        clock=Timeline(),
        sleep=lambda _seconds: None,
        send_reply=lambda *_a: None,
    )
    assert order == ["prep", "client:events", "sensor", "client:lambda", "reply", "status"]

    order.clear()
    rehearse(
        object(),
        "demo",
        "t1",
        "PH-2841",
        "RF-7781",
        (8.1, 8.6),
        4,
        "running",
        live=True,
        clock=Timeline(),
        sleep=lambda _seconds: None,
        send_reply=lambda *_a: None,
    )
    assert order == ["prep", "client:events", "sensor", "status"]


def test_other_tenant_executions_are_not_selected() -> None:
    assert incident_from_name("t1", "t1-INC-000012") == "INC-000012"
    assert incident_from_name("t1", "t2-INC-000012") is None
    assert child_line(None) == "child none"
    names = [f"S{index}" for index in range(10)]
    assert path_line(names) == "path " + " → ".join(names[-8:])
    assert reading_note(1.0, 2, 8, last=False) == "below safe range"
    assert reading_note(4, 2, 8, last=True) == "inside safe range"


def test_errors_drop_arns_and_numbers() -> None:
    assert public_error(RuntimeError(f"failed {COLD_ARN}")) == "demo command failed (RuntimeError)"
    assert public_error(RuntimeError(PHONE)) == "demo command failed (RuntimeError)"


def test_demo_is_not_a_ci_step() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts.demo" not in workflow
    assert "demo-run" not in workflow
    makefile = Path("Makefile").read_text(encoding="utf-8")
    for target in (
        "demo-preflight",
        "demo-prep",
        "demo-sensor",
        "demo-reply",
        "demo-status",
        "demo-cleanup",
        "demo-run",
    ):
        assert target in makefile
    assert "--tenant $(TENANT)" in makefile
    assert "--profile $(PROFILE)" in makefile
    assert "--account $(ACCOUNT)" in makefile
    assert "--button $(BUTTON)" in makefile


def _clients(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "dynamodb": object(),
        "stepfunctions": object(),
        "tenant_table": TENANT_TABLE,
        "routing_table": ROUTING_TABLE,
        "incident_table": INCIDENT_TABLE,
        "cold_chain_arn": COLD_ARN,
        "engage_party_arn": ENGAGE_ARN,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ready_session(
    *,
    seeded: bool,
    data_status: str = "CREATE_COMPLETE",
) -> tuple[Session, list[Stubber]]:
    sts, sts_stub = _aws("sts")
    sts_stub.add_response("get_caller_identity", _identity(ACCOUNT))
    formed, formed_stub = _aws("cloudformation")
    outputs = {
        "TenantTableName": TENANT_TABLE,
        "RoutingTableName": ROUTING_TABLE,
        "ColdChainStateMachineArn": COLD_ARN,
        "EngagePartyStateMachineArn": ENGAGE_ARN,
    }
    formed_stub.add_response("describe_stacks", _stack("OerfDataStack", data_status, outputs))
    for name in ("OerfComputeStack", "OerfApiStack", "OerfObservabilityStack"):
        formed_stub.add_response("describe_stacks", _stack(name, "UPDATE_COMPLETE"))
    steps, step_stub = _aws("stepfunctions")
    step_stub.add_response("describe_state_machine", _machine(COLD_ARN, "oerf-cold-chain"))
    step_stub.add_response("describe_state_machine", _machine(ENGAGE_ARN, "oerf-engage-party"))
    dynamo = FakeDynamoClient()
    if seeded:
        seed_runtime(dynamo, TENANT_TABLE, "t1", apply=True, routing_table=ROUTING_TABLE)
    session = Session(sts=sts, cloudformation=formed, stepfunctions=steps, dynamodb=dynamo)
    return session, [sts_stub, formed_stub, step_stub]


def _stub_range(stubber: Stubber) -> None:
    stubber.add_response(
        "get_item",
        {"Item": {**key("TENANT#t1", "SHIPMENT#PH-2841"), "product_profile": {"S": "PP-2-8-45"}}},
        {"TableName": TENANT_TABLE, "Key": key("TENANT#t1", "SHIPMENT#PH-2841")},
    )
    stubber.add_response(
        "get_item",
        {
            "Item": {
                **key("TENANT#t1", "CONFIG#cold_chain_excursion#ACTIVE"),
                "version": {"N": "1"},
            }
        },
        {"TableName": TENANT_TABLE, "Key": key("TENANT#t1", "CONFIG#cold_chain_excursion#ACTIVE")},
    )
    document = json.dumps({"product_profiles": [{"id": "PP-2-8-45", "range_c": [2, 8]}]})
    stubber.add_response(
        "get_item",
        {
            "Item": {
                **key("TENANT#t1", "CONFIG#cold_chain_excursion#v1"),
                "document": {"S": document},
                "version": {"N": "1"},
            }
        },
        {"TableName": TENANT_TABLE, "Key": key("TENANT#t1", "CONFIG#cold_chain_excursion#v1")},
    )


def _stub_child(stubber: Stubber, state: str) -> None:
    stubber.add_response(
        "list_executions",
        {"executions": [_execution_item("child-1", ENGAGE_ARN)]},
    )
    stubber.add_response(
        "describe_execution",
        _described(
            "child-1",
            ENGAGE_ARN,
            {"tenant_id": "t1", "incident_id": "INC-000012", "sender": PHONE},
        ),
    )
    stubber.add_response("get_execution_history", {"events": [_entered(1, state)]})


def _router(mock_whatsapp: str) -> dict[str, object]:
    return {
        "FunctionName": ROUTER_FUNCTION,
        "Environment": {"Variables": {"USE_MOCK_WHATSAPP": mock_whatsapp}},
    }


def _aws(service: str) -> tuple[object, Stubber]:
    client = boto3.client(service, region_name="us-east-1")
    stubber = Stubber(client)
    stubber.activate()
    return client, stubber


def _identity(account: str) -> dict[str, str]:
    return {"UserId": "AIDAHIDDEN", "Account": account, "Arn": ROLE_ARN}


def _stack(name: str, status: str, outputs: dict[str, str] | None = None) -> dict:
    stack: dict = {
        "StackName": name,
        "CreationTime": NOW,
        "StackStatus": status,
    }
    if outputs is not None:
        stack["Outputs"] = [
            {"OutputKey": key_name, "OutputValue": value} for key_name, value in outputs.items()
        ]
    return {"Stacks": [stack]}


def _machine(arn: str, name: str) -> dict:
    return {
        "stateMachineArn": arn,
        "name": name,
        "status": "ACTIVE",
        "definition": "{}",
        "roleArn": ROLE_ARN,
        "type": "STANDARD",
        "creationDate": NOW,
    }


def _execution_item(name: str, machine_arn: str) -> dict:
    return {
        "executionArn": f"arn:aws:states:us-east-1:000000000000:execution:machine:{name}",
        "stateMachineArn": machine_arn,
        "name": name,
        "status": "RUNNING",
        "startDate": NOW,
    }


def _described(name: str, machine_arn: str, body: dict) -> dict:
    return {
        "executionArn": f"arn:aws:states:us-east-1:000000000000:execution:machine:{name}",
        "stateMachineArn": machine_arn,
        "name": name,
        "status": "RUNNING",
        "startDate": NOW,
        "input": json.dumps(body),
    }


def _entered(event_id: int, name: str) -> dict:
    return {
        "timestamp": NOW,
        "type": "TaskStateEntered",
        "id": event_id,
        "stateEnteredEventDetails": {
            "name": name,
            "input": json.dumps({"sentinel": SENTINEL, "sender": PHONE}),
        },
    }
