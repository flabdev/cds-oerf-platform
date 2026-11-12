"""Stage guards for operator scripts. AWS clients are fakes. Nothing here calls the network."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from infra.naming import Stage
from scripts._stage import resolve_stage
from scripts.demo import main as demo_main
from scripts.demo_reply import main as reply_main
from scripts.dev_deploy import (
    CONTEXT_KEYS,
    context_arguments,
    deploy_context,
    main as deploy_main,
    resolve_inputs,
)
from scripts.dev_preflight import live_driver_parameter_names, main as preflight_main
from scripts.reset import main as reset_main
from scripts.seed_demo_users import main as users_main
from scripts.seed_runtime import main as seed_main
from scripts.verify_seed import main as verify_main
from scripts.whatsapp.templates import main as templates_main, url_button_proposal
from sim.temperature import main as temperature_main

ROOT = Path(__file__).resolve().parents[3]
ACCOUNT = "000000000000"
MAIN_ENV = {"ALLOW_MAIN": "1"}


@pytest.fixture(autouse=True)
def _clear_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STAGE", raising=False)
    monkeypatch.delenv("ALLOW_MAIN", raising=False)
    monkeypatch.delenv("DEMO_NUMBERS_PREFIX", raising=False)
    monkeypatch.delenv("ALARM_EMAIL", raising=False)
    monkeypatch.delenv("CHANNELS_LIVE", raising=False)


def test_stage_is_required_and_main_is_refused() -> None:
    with pytest.raises(SystemExit, match="stage is required"):
        resolve_stage(None, environ={})
    with pytest.raises(SystemExit, match="ALLOW_MAIN"):
        resolve_stage("main", environ={})
    with pytest.raises(SystemExit, match="ALLOW_MAIN"):
        resolve_stage(None, environ={"STAGE": "main"})


def test_dev_names_and_main_names() -> None:
    dev = resolve_stage("dev", environ={})
    main = resolve_stage("main", environ=MAIN_ENV)
    assert dev.data_stack == "OerfDataStack-dev"
    assert dev.compute_stack == "OerfComputeStack-dev"
    assert dev.api_stack == "OerfApiStack-dev"
    assert dev.observability_stack == "OerfObservabilityStack-dev"
    assert all(name.endswith("-dev") for name in dev.stacks)
    assert dev.event_bus == "oerf-dev-events"
    assert dev.cold_chain == "oerf-dev-cold-chain"
    assert dev.engage_party == "oerf-dev-engage-party"
    assert dev.router_function == "oerf-dev-channel-router"
    assert dev.inbound_function == "oerf-dev-inbound"
    assert dev.waba_parameter == "/oerf/dev/whatsapp/waba-id"
    assert dev.phone_number_id_parameter == "/oerf/dev/whatsapp/phone-number-id"
    assert dev.demo_numbers_prefix() == "/oerf/dev/demo-numbers"
    assert main.data_stack == "OerfDataStack"
    assert main.compute_stack == "OerfComputeStack"
    assert main.stacks == (
        "OerfDataStack",
        "OerfComputeStack",
        "OerfApiStack",
        "OerfObservabilityStack",
    )
    assert main.event_bus == "oerf-events"
    assert main.cold_chain == "oerf-cold-chain"
    assert main.engage_party == "oerf-engage-party"
    assert main.router_function == "oerf-channel-router"
    assert main.inbound_function == "oerf-inbound"
    assert main.waba_parameter == "/oerf/whatsapp/waba-id"
    assert main.phone_number_id_parameter == "/oerf/whatsapp/phone-number-id"
    assert main.demo_numbers_prefix() == "/oerf/demo-numbers"
    assert main.event_bus == Stage(name="main").res("events")


def test_demo_numbers_share_one_prefix_per_stage(capsys: pytest.CaptureFixture[str]) -> None:
    dev = resolve_stage("dev", environ={})
    main = resolve_stage("main", environ=MAIN_ENV)
    assert dev.driver_demo_number_name("t1", "C-T1-DRV") == (
        "/oerf/dev/demo-numbers/t1/driver/C-T1-DRV"
    )
    assert dev.demo_number_name("t1", "ops_manager") == "/oerf/dev/demo-numbers/t1/ops_manager"
    assert main.driver_demo_number_name("t1", "C-T1-DRV") == (
        "/oerf/demo-numbers/t1/driver/C-T1-DRV"
    )
    assert main.demo_number_name("t1", "qa") == "/oerf/demo-numbers/t1/qa"
    override = resolve_stage(
        "dev",
        environ={"DEMO_NUMBERS_PREFIX": "/oerf/lab/demo-numbers"},
    )
    assert override.driver_demo_number_name("t2", "C-T2-DRV") == (
        "/oerf/lab/demo-numbers/t2/driver/C-T2-DRV"
    )
    assert override.demo_number_name("t2", "facility") == "/oerf/lab/demo-numbers/t2/facility"
    assert "DEMO_NUMBERS_PREFIX override /oerf/lab/demo-numbers" in capsys.readouterr().err


def test_live_driver_names_follow_the_stage_prefix() -> None:
    names = live_driver_parameter_names("/oerf/dev/demo-numbers")
    assert "/oerf/dev/demo-numbers/t1/driver/C-T1-DRV" in names
    assert "/oerf/dev/demo-numbers/t1/driver/C-T1-DRV-ANANDA" in names
    assert "/oerf/dev/demo-numbers/t1/driver/C-T1-DRV-VIVEK" not in names
    assert all(name.startswith("/oerf/dev/demo-numbers/") for name in names)
    assert all("/driver/" in name for name in names)


_CASES = [
        (
            ["preflight", "--profile", "p", "--account", ACCOUNT, "--tenant", "t1"],
            demo_main,
            {"session_factory": None},
        ),
        (
            [
                "--profile",
                "p",
                "--tenant",
                "t1",
                "--shipment",
                "PH-1",
                "--role",
                "driver",
                "--text",
                "ok",
            ],
            reply_main,
            {},
        ),
        (["--profile", "p", "--tenant", "t1"], reset_main, {"session_factory": None}),
        (["--profile", "p", "--tenant", "t1"], seed_main, {}),
        (
            [
                "--profile",
                "p",
                "--tenant",
                "t1",
                "--role",
                "ops_user",
                "--email",
                "ops@example.com",
            ],
            users_main,
            {},
        ),
        (["--profile", "p", "--tenant", "t1"], verify_main, {}),
        (["lint"], templates_main, {}),
        (["--tenant", "t1", "--shipment", "PH-1"], temperature_main, {}),
]


@pytest.mark.parametrize(("argv", "runner", "kwargs"), _CASES)
def test_scripts_refuse_without_a_stage(argv: list[str], runner, kwargs: dict) -> None:
    with pytest.raises(SystemExit, match="stage is required"):
        runner(argv, **kwargs)


@pytest.mark.parametrize(("argv", "runner", "kwargs"), _CASES)
def test_scripts_refuse_main_without_allow(argv: list[str], runner, kwargs: dict) -> None:
    staged = [*argv, "--stage", "main"]
    with pytest.raises(SystemExit, match="ALLOW_MAIN"):
        runner(staged, **kwargs)


def test_dev_seed_touches_only_the_dev_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Recording()
    monkeypatch.setattr("boto3.Session", lambda **_: session)
    code = seed_main(["--profile", "p", "--tenant", "t1", "--stage", "dev"])
    assert code == 0
    assert session.stacks == ["OerfDataStack-dev"]
    assert "OerfDataStack" not in session.stacks
    joined = " ".join(session.stacks)
    assert not _mentions_main_stack(joined)


def test_dev_reset_and_verify_use_the_dev_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Recording()
    monkeypatch.setattr("boto3.Session", lambda **_: session)
    reset_code = reset_main(
        ["--profile", "p", "--tenant", "t1", "--stage", "dev"],
        session_factory=lambda _profile: session,
    )
    verify_code = verify_main(["--profile", "p", "--tenant", "t1", "--stage", "dev"])
    assert reset_code == 0
    assert verify_code == 1
    assert "OerfDataStack-dev" in session.stacks
    assert "OerfDataStack" not in session.stacks


def test_dev_reply_uses_dev_router_and_stacks(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Recording()
    monkeypatch.setattr("boto3.Session", lambda **_: session)
    with pytest.raises(SystemExit):
        reply_main(
            [
                "--profile",
                "p",
                "--tenant",
                "t1",
                "--shipment",
                "PH-1",
                "--role",
                "driver",
                "--text",
                "ok",
                "--stage",
                "dev",
            ]
        )
    assert session.functions == ["oerf-dev-channel-router"]
    assert session.stacks == ["OerfDataStack-dev", "OerfComputeStack-dev"]


def test_dev_preflight_prints_names_not_values(capsys: pytest.CaptureFixture[str]) -> None:
    session = _Recording(
        phone="phone-number-id-00000000000000000000000000000000",
        topic="arn:aws:sns:us-east-1:000000000000:inbound",
    )
    code = preflight_main(["--profile", "p", "--stage", "dev"], session_factory=lambda _p: session)
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS shared inbound topic" in out
    assert "arn:aws:sns" not in out
    assert session.phone not in out
    assert "/oerf/dev/whatsapp/phone-number-id" in out
    assert "/oerf/dev/demo-numbers/t1/driver/C-T1-DRV" in out
    assert "OerfComputeStack" in session.stacks
    dev_stacks = [name for name in session.stacks if name != "OerfComputeStack"]
    assert dev_stacks
    assert all(name.endswith("-dev") for name in dev_stacks)


def test_dev_demo_preflight_names_dev_stacks(capsys: pytest.CaptureFixture[str]) -> None:
    session = _Recording()
    code = demo_main(
        ["preflight", "--profile", "p", "--account", ACCOUNT, "--tenant", "t1", "--stage", "dev"],
        session_factory=lambda _p: session,
    )
    out = capsys.readouterr().out
    assert "stage=dev" in out
    assert "bus=oerf-dev-events" in out
    assert "OerfDataStack-dev" in out
    assert code in (0, 1)
    assert all(name.endswith("-dev") for name in session.stacks)
    assert "oerf-channel-router" not in session.functions


def test_dev_temperature_bus(capsys: pytest.CaptureFixture[str]) -> None:
    code = temperature_main(["--tenant", "t1", "--shipment", "PH-1", "--stage", "dev"])
    captured = capsys.readouterr()
    assert code == 0
    assert "bus=oerf-dev-events" in captured.err
    assert '"EventBusName": "oerf-dev-events"' in captured.out
    assert "oerf-events" not in captured.out.replace("oerf-dev-events", "")


def test_main_temperature_with_allow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ALLOW_MAIN", "1")
    code = temperature_main(["--tenant", "t1", "--shipment", "PH-1", "--stage", "main"])
    captured = capsys.readouterr()
    assert code == 0
    assert '"EventBusName": "oerf-events"' in captured.out


def test_url_button_proposal_names_the_dev_copies() -> None:
    text = url_button_proposal("dev")
    assert "oerf_ops_escalation_options" in text
    assert "oerf_qa_report_ready" in text
    assert "oerf_dev_ops_escalation_options" in text
    assert "oerf_dev_qa_report_ready" in text
    assert "DashboardUrl" in text
    assert "only the copies" in text
    assert "not submitted from dev" in text


def _gh(*rows: tuple[str, str | None, str]):
    table = {(name, environment): value for name, environment, value in rows}

    def get(name: str, *, environment: str | None) -> str:
        return table.get((name, environment), "")

    return get


def test_mock_deploy_on_a_fresh_stack_omits_empty_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = resolve_inputs(
        _Recording(),
        account=ACCOUNT,
        environ={},
        gh_get=_gh(),
    )
    err = capsys.readouterr().err
    assert context["channels:live"] == "false"
    assert context["whatsapp:stageUrlTemplates"] == "false"
    assert "whatsapp:phoneNumberId" not in context
    assert "whatsapp:sharedInboundTopicArn" not in context
    assert "alarms:email" not in context
    assert "whatsapp:wabaId" not in context
    assert "WA_PHONE_NUMBER_ID empty omitted" in err
    assert "ALARM_EMAIL empty omitted" in err
    assert "SHARED_INBOUND_TOPIC_ARN empty omitted" in err
    assert "CHANNELS_LIVE empty set" in err
    arguments = context_arguments(context)
    assert "OerfComputeStack-dev" in arguments
    assert not any(part.startswith("whatsapp:phoneNumberId=") for part in arguments)
    assert not any(part.startswith("alarms:email=") for part in arguments)


def test_channels_live_without_a_phone_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="WA_PHONE_NUMBER_ID"):
        resolve_inputs(
            _Recording(),
            account=ACCOUNT,
            environ={"CHANNELS_LIVE": "true", "DEV_DEPLOY_ALLOW_OMIT": "1"},
            gh_get=_gh(),
        )
    assert "phone-number" not in capsys.readouterr().err


def test_deployed_values_refuse_an_empty_resolution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    phone = "phone-number-id-00000000000000000000000000000000"
    session = _Recording(router_phone=phone, alarm_subscription=True)
    with pytest.raises(SystemExit) as caught:
        resolve_inputs(session, account=ACCOUNT, environ={}, gh_get=_gh())
    message = str(caught.value)
    assert "widen the router permission" in message
    assert "delete the subscription" in message
    assert "DEV_DEPLOY_ALLOW_OMIT=1" in message
    assert phone not in message
    assert phone not in capsys.readouterr().err

    allowed = resolve_inputs(
        _Recording(router_phone=phone, alarm_subscription=True),
        account=ACCOUNT,
        environ={"DEV_DEPLOY_ALLOW_OMIT": "1"},
        gh_get=_gh(),
    )
    err = capsys.readouterr().err
    assert "whatsapp:phoneNumberId" not in allowed
    assert "alarms:email" not in allowed
    assert phone not in err
    assert "WA_PHONE_NUMBER_ID empty omitted" in err
    assert "ALARM_EMAIL empty omitted" in err


def test_shared_inbound_drift_refuses_an_empty_topic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main_topic = "arn:aws:sns:us-east-1:000000000000:OerfComputeStack-InboundTopicABC"
    dev_topic = "arn:aws:sns:us-east-1:000000000000:OerfComputeStack-dev-InboundTopicABC"
    with pytest.raises(SystemExit) as caught:
        resolve_inputs(
            _Recording(inbound_topic_arn=main_topic),
            account=ACCOUNT,
            environ={},
            gh_get=_gh(),
        )
    message = str(caught.value)
    assert (
        "would move the dev inbound subscription off main's topic; "
        "dev would stop receiving WhatsApp events"
    ) in message
    assert "DEV_DEPLOY_ALLOW_OMIT=1" in message
    assert main_topic not in message
    assert main_topic not in capsys.readouterr().err

    allowed = resolve_inputs(
        _Recording(inbound_topic_arn=main_topic),
        account=ACCOUNT,
        environ={"DEV_DEPLOY_ALLOW_OMIT": "1"},
        gh_get=_gh(),
    )
    assert "whatsapp:sharedInboundTopicArn" not in allowed

    kept = resolve_inputs(
        _Recording(inbound_topic_arn=main_topic),
        account=ACCOUNT,
        environ={"SHARED_INBOUND_TOPIC_ARN": main_topic},
        gh_get=_gh(),
    )
    assert kept["whatsapp:sharedInboundTopicArn"] == main_topic
    assert main_topic not in capsys.readouterr().err

    own_topic = resolve_inputs(
        _Recording(inbound_topic_arn=dev_topic),
        account=ACCOUNT,
        environ={},
        gh_get=_gh(),
    )
    assert "whatsapp:sharedInboundTopicArn" not in own_topic


def test_dev_deploy_resolution_order(capsys: pytest.CaptureFixture[str]) -> None:
    phone_env = "phone-number-id-11111111111111111111111111111111"
    phone_repo = "phone-number-id-22222222222222222222222222222222"
    phone_ssm = "phone-number-id-33333333333333333333333333333333"
    topic = "arn:aws:sns:us-east-1:000000000000:inbound"
    asked: list[tuple[str, str | None]] = []

    def gh_get(name: str, *, environment: str | None) -> str:
        asked.append((name, environment))
        return _gh(
            ("WA_PHONE_NUMBER_ID", "dev", phone_env),
            ("WA_PHONE_NUMBER_ID", None, phone_repo),
            ("ALARM_EMAIL", None, "ops@example.com"),
            ("CHANNELS_LIVE", "dev", "true"),
            ("CHANNELS_LIVE", None, "false"),
        )(name, environment=environment)

    session = _Recording(phone=phone_ssm)
    context = resolve_inputs(session, account=ACCOUNT, environ={}, gh_get=gh_get)
    err = capsys.readouterr().err
    assert context["whatsapp:phoneNumberId"] == phone_env
    assert context["alarms:email"] == "ops@example.com"
    assert context["channels:live"] == "true"
    assert "whatsapp:sharedInboundTopicArn" not in context
    assert "WA_PHONE_NUMBER_ID gh env set" in err
    assert "ALARM_EMAIL gh repo set" in err
    assert "SHARED_INBOUND_TOPIC_ARN empty omitted" in err
    assert "CHANNELS_LIVE gh env set" in err
    assert ("WA_PHONE_NUMBER_ID", None) not in asked
    assert session.parameters == []
    for secret in (phone_env, phone_repo, phone_ssm, "ops@example.com"):
        assert secret not in err

    ssm_only = resolve_inputs(
        _Recording(phone=phone_ssm),
        account=ACCOUNT,
        environ={},
        gh_get=_gh(),
    )
    assert ssm_only["whatsapp:phoneNumberId"] == phone_ssm
    assert "WA_PHONE_NUMBER_ID SSM set" in capsys.readouterr().err

    def refuse_gh(name: str, *, environment: str | None) -> str:
        raise AssertionError(name)

    overridden = resolve_inputs(
        _Recording(phone=phone_ssm),
        account=ACCOUNT,
            environ={
                "WA_PHONE_NUMBER_ID": phone_env,
                "ALARM_EMAIL": "override@example.com",
                "SHARED_INBOUND_TOPIC_ARN": topic,
                "CHANNELS_LIVE": "false",
                "STAGE_URL_TEMPLATES": "false",
            },
        gh_get=refuse_gh,
    )
    err = capsys.readouterr().err
    assert overridden["whatsapp:phoneNumberId"] == phone_env
    assert overridden["alarms:email"] == "override@example.com"
    assert overridden["whatsapp:sharedInboundTopicArn"] == topic
    assert overridden["channels:live"] == "false"
    assert overridden["whatsapp:stageUrlTemplates"] == "false"
    assert "WA_PHONE_NUMBER_ID override set" in err
    assert "ALARM_EMAIL override set" in err
    assert "SHARED_INBOUND_TOPIC_ARN override set" in err
    assert "CHANNELS_LIVE override set" in err
    assert "STAGE_URL_TEMPLATES override set" in err
    assert phone_env not in err
    assert topic not in err
    assert list(overridden) == [key for key in CONTEXT_KEYS if key in overridden]


def test_dev_deploy_command_on_a_fresh_stack(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _refuse_cdk(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cdk")

    monkeypatch.setattr("scripts.dev_deploy.subprocess.run", _refuse_cdk)
    session = _Recording()
    code = deploy_main(
        ["check", "--profile", "p", "--account", ACCOUNT],
        session_factory=lambda _profile: session,
        gh_get=_gh(),
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "dev-deploy values present" in captured.out
    assert "WA_PHONE_NUMBER_ID empty omitted" in captured.err
    assert session.stacks == ["OerfObservabilityStack-dev", "OerfComputeStack-dev"]
    assert "OerfComputeStack" not in session.stacks


def test_dev_deploy_context_matches_the_workflow() -> None:
    context = deploy_context(
        account=ACCOUNT,
        phone_id="phone-number-id-00000000000000000000000000000000",
        topic_arn="arn:aws:sns:us-east-1:000000000000:inbound",
        alarm_email="ops@example.com",
        channels_live="false",
    )
    assert list(context) == list(CONTEXT_KEYS)
    assert context["stage"] == "dev"
    assert context["agentcore:enabled"] == "true"
    assert context["dashboard:enabled"] == "true"
    assert context["desk:publish"] == "true"
    assert context["auth:managedLogin"] == "true"
    assert context["channels:live"] == "false"
    assert context["whatsapp:stageUrlTemplates"] == "false"
    assert "whatsapp:wabaId" not in context


def test_make_targets_require_stage_and_dev_sensor_uses_the_dev_stage() -> None:
    missing = _make("demo-preflight", "PROFILE=p", "TENANT=t1", "ACCOUNT=000000000000")
    assert missing.returncode != 0
    assert "STAGE is required" in missing.stderr
    refused = _make(
        "demo-sensor",
        "STAGE=main",
        "PROFILE=p",
        "TENANT=t1",
        "ACCOUNT=000000000000",
    )
    assert refused.returncode != 0
    assert "ALLOW_MAIN" in refused.stderr
    sensor = _make(
        "dev-sensor",
        "PROFILE=p",
        "ACCOUNT=000000000000",
        "TENANT=t1",
        "SHIPMENT=PH-1",
        "DEVICE=RF-1",
        "SCENARIO=excursion",
    )
    assert sensor.returncode == 0
    assert "--stage dev" in sensor.stdout
    assert "--scenario excursion" in sensor.stdout
    assert "oerf-events" not in sensor.stdout


def _make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-n", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _mentions_main_stack(text: str) -> bool:
    for name in ("OerfDataStack", "OerfComputeStack", "OerfApiStack", "OerfObservabilityStack"):
        if name in text and f"{name}-dev" not in text:
            return True
    return False


def _machine(name: str) -> str:
    return f"arn:aws:states:us-east-1:0:stateMachine:{name}"


class _Recording:
    """Enough of a session for a dry run. It records names and writes nothing."""

    def __init__(
        self,
        *,
        phone: str = "",
        topic: str = "",
        router_phone: str = "",
        alarm_subscription: bool = False,
        inbound_topic_arn: str = "",
    ) -> None:
        self.stacks: list[str] = []
        self.functions: list[str] = []
        self.parameters: list[str] = []
        self.phone = phone
        self.topic = topic
        self.router_phone = router_phone
        self.alarm_subscription = alarm_subscription
        self.inbound_topic_arn = inbound_topic_arn

    def client(self, _service: str, **_kwargs: object) -> _Recording:
        return self

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": ACCOUNT}

    def describe_stacks(self, *, StackName: str) -> dict[str, object]:
        self.stacks.append(StackName)
        outputs = [
            {"OutputKey": "TenantTableName", "OutputValue": "tenant-dev"},
            {"OutputKey": "RoutingTableName", "OutputValue": "routing-dev"},
            {"OutputKey": "IncidentTableName", "OutputValue": "incident-dev"},
            {"OutputKey": "EvidenceBucketName", "OutputValue": "evidence-dev"},
            {
                "OutputKey": "ColdChainStateMachineArn",
                "OutputValue": _machine("oerf-dev-cold-chain"),
            },
            {
                "OutputKey": "EngagePartyStateMachineArn",
                "OutputValue": _machine("oerf-dev-engage-party"),
            },
            {"OutputKey": "InboundQueueUrl", "OutputValue": "https://example.com/queue"},
            {"OutputKey": "CognitoUserPoolId", "OutputValue": "us-east-1_pool"},
        ]
        return {"Stacks": [{"StackStatus": "UPDATE_COMPLETE", "Outputs": outputs}]}

    def describe_state_machine(self, *, stateMachineArn: str) -> dict[str, str]:
        name = stateMachineArn.rsplit(":", 1)[-1]
        return {"name": name, "status": "ACTIVE"}

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, object]:
        self.functions.append(FunctionName)
        variables = {"USE_MOCK_WHATSAPP": "true"}
        if self.router_phone and FunctionName == "oerf-dev-channel-router":
            variables["WHATSAPP_PHONE_NUMBER_ID"] = self.router_phone
        return {"Environment": {"Variables": variables}}

    def get_parameter(self, *, Name: str, WithDecryption: bool = False) -> dict[str, object]:
        self.parameters.append(Name)
        if not self.phone:
            raise _Missing()
        return {"Parameter": {"Name": Name, "Value": self.phone}}

    def list_stack_resources(self, **kwargs: str) -> dict[str, object]:
        self.stacks.append(kwargs["StackName"])
        summaries: list[dict[str, str]] = []
        if kwargs["StackName"] == "OerfComputeStack" and self.topic:
            summaries.append(
                {
                    "ResourceType": "AWS::SNS::Topic",
                    "LogicalResourceId": "InboundTopic",
                    "PhysicalResourceId": self.topic,
                }
            )
        if kwargs["StackName"] == "OerfComputeStack-dev" and self.inbound_topic_arn:
            summaries.append(
                {
                    "ResourceType": "AWS::SNS::Subscription",
                    "LogicalResourceId": "SharedInbound",
                    "PhysicalResourceId": "arn:aws:sns:us-east-1:000000000000:pending:sub",
                }
            )
        if kwargs["StackName"] == "OerfObservabilityStack-dev" and self.alarm_subscription:
            summaries.append(
                {
                    "ResourceType": "AWS::SNS::Subscription",
                    "LogicalResourceId": "AlarmEmail",
                    "PhysicalResourceId": "subscription",
                }
            )
        return {"StackResourceSummaries": summaries}

    def get_subscription_attributes(self, *, SubscriptionArn: str) -> dict[str, object]:
        return {
            "Attributes": {
                "SubscriptionArn": SubscriptionArn,
                "TopicArn": self.inbound_topic_arn,
            }
        }

    def get_item(self, **_kwargs: object) -> dict[str, object]:
        return {}

    def scan(self, **_kwargs: object) -> dict[str, object]:
        return {"Items": []}

    def list_executions(self, **_kwargs: object) -> dict[str, object]:
        return {"executions": []}

    def list_objects_v2(self, **_kwargs: object) -> dict[str, object]:
        return {"Contents": [], "IsTruncated": False}

    def admin_get_user(self, **_kwargs: object) -> dict[str, object]:
        raise _Missing()


class _Missing(Exception):
    response = {"Error": {"Code": "ParameterNotFound"}}
