"""Run one tenant's cold-chain demo from the operator laptop.

Operator tool. It is not a Lambda and it is not part of CI. Reset, the runtime
seed, and the driver reply are function calls, not shell commands.

    python -m scripts.demo preflight --profile <profile> --account <account-id> --tenant t1
    python -m scripts.demo run --profile <profile> --account <account-id> --tenant t1

``--profile`` and ``--account`` are required. The region is ``us-east-1``.
``--shipment`` and ``--device`` default to that tenant's seeded shipment.
Every command refuses a caller that is not the demo account before it reads
or writes anything else. Phone numbers, task tokens, ARNs, and raw payloads
are not printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from oerf.platform.agent.context import DOMAIN
from oerf.platform.channels.address import demo_number_name
from oerf.platform.ingest.handler import DETAIL_TYPE, SOURCE
from oerf.platform.storage.dynamo import incident_pk, iso_z, key
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from scripts.demo_reply import (
    ROUTER_FUNCTION,
    channels_live_from_configuration,
    main as post_reply,
)
from scripts.reset import REGION, STACK as DATA_STACK, clients_from_session, reset_tenant
from scripts.seed_runtime import seed_runtime, tenants_for
from sim.seed.load import load_seed

EVENT_BUS = "oerf-events"
EVENT_GAP = timedelta(seconds=30)
SENSOR_TIMEOUT = 30.0
REPLY_TIMEOUT = 120.0
POLL_SECONDS = 1.0
READING_LAT = 17.44
READING_LON = 78.38
STACKS = (
    "OerfDataStack",
    "OerfComputeStack",
    "OerfApiStack",
    "OerfObservabilityStack",
)
MACHINES = (
    ("ColdChainStateMachineArn", "oerf-cold-chain"),
    ("EngagePartyStateMachineArn", "oerf-engage-party"),
)
PROMPT_STATE = "SendViaRouter"
_LEAK = re.compile(r"arn:aws|\+\d{8,}|tasktoken|task token|EventId|DetailType", re.IGNORECASE)
_INCIDENT_NAME = re.compile(r"INC-\d{6}")

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
ReplySender = Callable[[str, str, str, str], None]


class DemoError(Exception):
    """A demo command failed. The message is safe to print."""


@dataclass(frozen=True)
class Execution:
    """One state-machine execution. ``str`` is the name, never the ARN."""

    name: str
    status: str
    arn: str

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Execution({self.name}, {self.status})"


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[str], Any] | None = None,
    clock: Clock | None = None,
    sleep: Sleeper | None = None,
    send_reply: ReplySender | None = None,
) -> int:
    """Dispatch one subcommand. Returns 0 on success."""

    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        account = parse_account(args.account)
        tenant_id = tenants_for(args.tenant)[0]
        shipment_id, device_id = resolve_subject(tenant_id, args.shipment, args.device)
        session = (session_factory or _session)(args.profile)
        now = clock or _now
        pause_for = sleep or time.sleep
        sender = send_reply or send_driver_reply
        if args.command == "preflight":
            return preflight(session, tenant_id, account=account, live=args.live)
        require_demo_account(session, account)
        if args.command == "reply" and args.live:
            raise DemoError("reply is for mocked runs")
        if args.command == "prep":
            _prepare_session(session, tenant_id)
        elif args.command == "sensor":
            publish_excursion(
                _resources(session),
                _client(session, "events"),
                tenant_id,
                shipment_id,
                device_id,
                parse_temps(args.temps),
                parse_pause(args.pause),
                now,
                pause_for,
            )
        elif args.command == "reply":
            post_driver_reply(
                _resources(session),
                _client(session, "lambda"),
                args.profile,
                tenant_id,
                shipment_id,
                args.button,
                live=args.live,
                clock=now,
                sleep=pause_for,
                send_reply=sender,
            )
        elif args.command == "status":
            show_status(_resources(session), tenant_id)
        elif args.command == "cleanup":
            _cleanup_session(session, tenant_id)
        elif args.command == "run":
            rehearse(
                session,
                args.profile,
                tenant_id,
                shipment_id,
                device_id,
                parse_temps(args.temps),
                parse_pause(args.pause),
                args.button,
                live=args.live,
                clock=now,
                sleep=pause_for,
                send_reply=sender,
            )
        else:
            raise DemoError(f"unknown command {args.command}")
    except DemoError as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    return 0


def resolve_subject(tenant_id: str, shipment: str | None, device: str | None) -> tuple[str, str]:
    """Shipment and device from the flags, or that tenant's seed row."""

    if shipment and device:
        return shipment, device
    rows = load_seed(tenant_id).get("shipments") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_shipment = str(row.get("shipment_id") or "").strip()
        row_device = str(row.get("device") or "").strip()
        if shipment and row_shipment != shipment:
            continue
        if device and row_device != device:
            continue
        if row_shipment and row_device:
            return row_shipment, row_device
    raise DemoError("the tenant seed has no shipment for this demo")


def parse_temps(raw: str) -> tuple[float, ...]:
    """Comma-separated readings. Two or more, so debounce can open an incident."""

    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) < 2:
        raise DemoError("--temps needs at least two readings")
    values: list[float] = []
    for part in parts:
        try:
            number = float(part)
        except ValueError:
            raise DemoError("--temps must be numbers") from None
        if number != number or number in {float("inf"), float("-inf")}:
            raise DemoError("--temps must be finite")
        values.append(number)
    return tuple(values)


def parse_account(value: str) -> str:
    """A 12-digit account id. The value is never printed."""

    if len(value) != 12 or not value.isdigit():
        raise DemoError("--account must be a 12-digit account id")
    return value


def parse_pause(value: float) -> float:
    """Seconds of wall time between published readings."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise DemoError("--pause must be a number of seconds") from None
    if number < 0 or number != number or number > 3600:
        raise DemoError("--pause must be between 0 and 3600 seconds")
    return number


def preflight(session: Any, tenant_id: str, *, account: str, live: bool) -> int:
    """Print one PASS or FAIL line per check. A wrong account stops immediately."""

    caller = _caller_account(session)
    if caller != account:
        say("FAIL account")
        return 1
    say("PASS account")
    failed = False
    formed = _client(session, "cloudformation")
    outputs: dict[str, str] = {}
    for name in STACKS:
        status, found = read_stack(formed, name)
        if status is not None and status.endswith("_COMPLETE"):
            say(f"PASS stack {name}")
        elif status is None:
            say(f"FAIL stack {name} missing")
            failed = True
        else:
            say(f"FAIL stack {name} {status}")
            failed = True
        if name == DATA_STACK:
            outputs = found
    steps = _client(session, "stepfunctions")
    for output_key, machine in MACHINES:
        arn = outputs.get(output_key) or ""
        if arn and machine_active(steps, arn, machine):
            say(f"PASS state machine {machine}")
        else:
            say(f"FAIL state machine {machine}")
            failed = True
    tenant_table = outputs.get("TenantTableName") or ""
    routing_table = outputs.get("RoutingTableName") or ""
    if not tenant_table or not routing_table:
        say("FAIL runtime rows")
        failed = True
    else:
        try:
            counts = seed_runtime(
                _client(session, "dynamodb"),
                tenant_table,
                tenant_id,
                apply=False,
                routing_table=routing_table,
            )
        except Exception as exc:
            say("FAIL runtime rows")
            print(public_error(exc), file=sys.stderr)
            failed = True
        else:
            changed = sum(counts.values())
            if changed:
                say(f"FAIL runtime rows ({changed} would change)")
                failed = True
            else:
                say("PASS runtime rows")
    if live:
        try:
            present = driver_parameter_exists(_client(session, "ssm"), tenant_id)
        except DemoError as exc:
            say("FAIL driver parameter")
            print(public_error(exc), file=sys.stderr)
            failed = True
        else:
            say("PASS driver parameter" if present else "FAIL driver parameter")
            failed = failed or not present
    return 1 if failed else 0


def prepare(
    clients: Any,
    tenant_id: str,
    *,
    reset_fn: Callable[..., Mapping[str, int]] = reset_tenant,
    seed_fn: Callable[..., Mapping[str, int]] = seed_runtime,
) -> None:
    """Apply a reseeded reset, write the runtime rows, then require a clean dry run."""

    reset_fn(clients, tenant_id, apply=True, reseed=True)
    seed_fn(
        clients.dynamodb,
        clients.tenant_table,
        tenant_id,
        apply=True,
        routing_table=clients.routing_table,
    )
    _require_clear(
        reset_fn(clients, tenant_id, apply=False, reseed=True),
        f"prep {tenant_id} left",
    )
    say(f"prep {tenant_id} ready")


def cleanup(
    clients: Any,
    tenant_id: str,
    *,
    reset_fn: Callable[..., Mapping[str, int]] = reset_tenant,
) -> None:
    """Reset the tenant, then require the dry run to report zero."""

    reset_fn(clients, tenant_id, apply=True, reseed=False)
    _require_clear(
        reset_fn(clients, tenant_id, apply=False, reseed=False),
        f"cleanup {tenant_id} left",
    )
    say(f"cleanup {tenant_id} clear")


def event_moments(end: datetime, count: int) -> list[datetime]:
    """Event times ending at ``end``, thirty seconds apart."""

    if count < 1:
        raise DemoError("at least one reading is required")
    start = end - EVENT_GAP * (count - 1)
    return [start + EVENT_GAP * index for index in range(count)]


def reading_entry(
    *,
    tenant_id: str,
    shipment_id: str,
    device_id: str,
    moment: datetime,
    temperature_c: float,
    seq: int,
) -> dict[str, Any]:
    """One ``sim.reefer`` reading in the run-sheet detail shape."""

    stamp = iso_z(moment)
    detail = {
        "tenant_id": tenant_id,
        "shipment_id": shipment_id,
        "device_id": device_id,
        "ts": stamp,
        "temperature_c": temperature_c,
        "lat": READING_LAT,
        "lon": READING_LON,
        "seq": seq,
    }
    return {
        "Source": SOURCE,
        "DetailType": DETAIL_TYPE,
        "EventBusName": EVENT_BUS,
        "Time": moment.astimezone(UTC),
        "Detail": json.dumps(detail),
    }


def publish_excursion(
    clients: Any,
    events: Any,
    tenant_id: str,
    shipment_id: str,
    device_id: str,
    temps: tuple[float, ...],
    pause: float,
    clock: Clock,
    sleep: Sleeper,
    timeout: float = SENSOR_TIMEOUT,
) -> str:
    """Publish the readings and wait until a new cold-chain execution exists."""

    low, high = safe_range(clients.dynamodb, clients.tenant_table, tenant_id, shipment_id)
    say(
        "OERF reefer simulator · "
        f"tenant {tenant_id} · shipment {shipment_id} · device {device_id} · "
        f"safe range {range_text(low, high)}"
    )
    end = clock()
    moments = event_moments(end, len(temps))
    known = {
        item.name
        for item in _for_tenant(
            _list_executions(clients.stepfunctions, clients.cold_chain_arn),
            tenant_id,
        )
    }
    last = len(temps) - 1
    for index, (temperature, moment) in enumerate(zip(temps, moments, strict=True)):
        accept_reading(
            events,
            reading_entry(
                tenant_id=tenant_id,
                shipment_id=shipment_id,
                device_id=device_id,
                moment=moment,
                temperature_c=temperature,
                seq=index + 1,
            ),
        )
        say(
            sensor_line(
                clock(),
                device_id,
                temperature,
                reading_note(temperature, low, high, last=index == last),
            )
        )
        if index != last:
            sleep(pause)
    opened = poll(
        clock,
        sleep,
        timeout,
        lambda: _new_execution(
            clients.stepfunctions,
            clients.cold_chain_arn,
            tenant_id,
            known,
        ),
    )
    if opened is None:
        raise DemoError(f"no cold-chain execution opened for {tenant_id}")
    incident_id = incident_from_name(tenant_id, opened.name)
    if incident_id is None:
        raise DemoError(f"no cold-chain execution opened for {tenant_id}")
    say(incident_line(clock(), incident_id))
    return incident_id


def post_driver_reply(
    clients: Any,
    lambda_client: Any,
    profile: str,
    tenant_id: str,
    shipment_id: str,
    button: str,
    *,
    live: bool,
    clock: Clock,
    sleep: Sleeper,
    send_reply: ReplySender,
    incident_id: str | None = None,
    assume_mocked: bool = False,
    timeout: float = REPLY_TIMEOUT,
) -> None:
    """Wait until the engage-party child is at SendViaRouter, then post the reply."""

    if live:
        raise DemoError("reply is for mocked runs")
    if button not in {"running", "stopped"}:
        raise DemoError("button must be running or stopped")
    if not assume_mocked and router_is_live(lambda_client):
        raise DemoError("reply is for mocked runs")
    if incident_id is None:
        parent = newest_parent(clients.stepfunctions, clients.cold_chain_arn, tenant_id)
        incident_id = incident_from_name(tenant_id, parent.name)
        if incident_id is None:
            raise DemoError(f"no cold-chain execution for {tenant_id}")
    found = poll(
        clock,
        sleep,
        timeout,
        lambda: _prompt_ready(clients, tenant_id, incident_id),
    )
    if not found:
        raise DemoError("the driver prompt did not reach SendViaRouter")
    send_reply(profile, tenant_id, shipment_id, button)


def show_status(clients: Any, tenant_id: str) -> None:
    """Print the parent, the child, the last eight parent states, and the incident."""

    parent = newest_parent(clients.stepfunctions, clients.cold_chain_arn, tenant_id)
    incident_id = incident_from_name(tenant_id, parent.name)
    if incident_id is None:
        raise DemoError(f"no cold-chain execution for {tenant_id}")
    path = execution_path(clients.stepfunctions, parent.arn)
    child = child_for(clients.stepfunctions, clients.engage_party_arn, tenant_id, incident_id)
    state = incident_state(clients.dynamodb, clients.incident_table, tenant_id, incident_id)
    for line in (
        f"parent {parent.name} {parent.status}",
        child_line(child),
        path_line(path),
        f"incident {incident_id} {state}",
    ):
        say(line)


def rehearse(
    session: Any,
    profile: str,
    tenant_id: str,
    shipment_id: str,
    device_id: str,
    temps: tuple[float, ...],
    pause: float,
    button: str,
    *,
    live: bool,
    clock: Clock,
    sleep: Sleeper,
    send_reply: ReplySender,
) -> None:
    """Prep, publish the excursion, post the mocked reply, then print status."""

    clients = _resources(session)
    prepare(clients, tenant_id)
    publish_excursion(
        clients,
        _client(session, "events"),
        tenant_id,
        shipment_id,
        device_id,
        temps,
        pause,
        clock,
        sleep,
    )
    if not live:
        functions = _client(session, "lambda")
        if router_is_live(functions):
            raise DemoError("reply is for mocked runs")
        post_driver_reply(
            clients,
            functions,
            profile,
            tenant_id,
            shipment_id,
            button,
            live=False,
            clock=clock,
            sleep=sleep,
            send_reply=send_reply,
            assume_mocked=True,
        )
    show_status(clients, tenant_id)


def require_demo_account(session: Any, account: str) -> None:
    """Stop before any other call when the caller is not the given account."""

    if _caller_account(session) != account:
        raise DemoError("refusing account; this demo only runs in the demo account")


def read_stack(cloudformation: Any, name: str) -> tuple[str | None, dict[str, str]]:
    """Return one stack's status and outputs. A missing stack is ``(None, {})``."""

    try:
        response = cloudformation.describe_stacks(StackName=name)
    except Exception as exc:
        if _error_code(exc) in {"ValidationError", "StackNotFoundException"}:
            return None, {}
        raise DemoError(f"could not read stack {name}") from None
    stacks = response.get("Stacks") if isinstance(response, dict) else None
    if not isinstance(stacks, list) or not stacks:
        return None, {}
    stack = stacks[0]
    status = stack.get("StackStatus")
    outputs = {
        str(item.get("OutputKey")): str(item.get("OutputValue") or "").strip()
        for item in stack.get("Outputs") or []
        if isinstance(item, dict) and item.get("OutputKey")
    }
    return status if isinstance(status, str) else None, outputs


def machine_active(stepfunctions: Any, arn: str, expected_name: str) -> bool:
    """True when that state machine exists and is ACTIVE. The ARN is not printed."""

    try:
        described = stepfunctions.describe_state_machine(stateMachineArn=arn)
    except Exception:
        return False
    if not isinstance(described, dict):
        return False
    return described.get("name") == expected_name and described.get("status") == "ACTIVE"


def driver_parameter_exists(ssm: Any, tenant_id: str) -> bool:
    """True when the driver parameter is present. The value is discarded."""

    parameter_name = demo_number_name(tenant_id, "driver")
    try:
        found = ssm.get_parameter(Name=parameter_name, WithDecryption=True)
    except Exception as exc:
        if _error_code(exc) == "ParameterNotFound":
            return False
        raise DemoError("could not read the driver parameter") from None
    parameter = found.get("Parameter") if isinstance(found, dict) else None
    if not isinstance(parameter, dict):
        return False
    parameter.pop("Value", None)
    return bool(parameter.get("Name"))


def safe_range(dynamodb: Any, table: str, tenant_id: str, shipment_id: str) -> tuple[float, float]:
    """The shipment profile's ``range_c`` from the published tenant config."""

    shipment = dynamodb.get_item(
        TableName=table,
        Key=key(f"TENANT#{tenant_id}", f"SHIPMENT#{shipment_id}"),
    ).get("Item")
    profile_id = _text(shipment, "product_profile") or _text(shipment, "product_profile_id")
    if not profile_id:
        raise DemoError(f"shipment {shipment_id} has no product profile")
    document = DynamoConfigRepository(dynamodb, table).read_tenant_config(tenant_id, DOMAIN)
    band = _range_for(document, profile_id)
    if band is None:
        raise DemoError(f"shipment {shipment_id} has no safe range")
    return band


def accept_reading(events: Any, entry: Mapping[str, Any]) -> None:
    """Publish one reading. A rejected entry is an error without the API body."""

    try:
        result = events.put_events(Entries=[dict(entry)])
    except Exception:
        raise DemoError("the reading was not accepted") from None
    failed = result.get("FailedEntryCount") if isinstance(result, dict) else 1
    if failed:
        raise DemoError("the reading was not accepted")


def sensor_line(moment: datetime, device_id: str, temperature: float, note: str) -> str:
    """One on-camera reading line. The time is the local wall clock."""

    return f"{moment.astimezone().strftime('%H:%M:%S')}  {device_id}  {temperature:.1f} °C  {note}"


def incident_line(moment: datetime, incident_id: str) -> str:
    return (
        f"{moment.astimezone().strftime('%H:%M:%S')}  "
        f"Incident {incident_id} open · waiting for the driver on WhatsApp"
    )


def reading_note(temperature: float, low: float, high: float, *, last: bool) -> str:
    """How this reading sits against the safe range. The last breach opens the incident."""

    if temperature > high:
        breach = "above safe range"
    elif temperature < low:
        breach = "below safe range"
    else:
        breach = "inside safe range"
    if last and breach != "inside safe range":
        return "excursion confirmed → incident opening…"
    return breach


def range_text(low: float, high: float) -> str:
    return f"{_number(low)}–{_number(high)} °C"


def child_line(found: tuple[str, str] | None) -> str:
    if found is None:
        return "child none"
    status, state = found
    if state:
        return f"child {status} {state}"
    return f"child {status}"


def path_line(names: list[str]) -> str:
    shown = names[-8:]
    if not shown:
        return "path none"
    return "path " + " → ".join(shown)


def incident_from_name(tenant_id: str, name: str) -> str | None:
    """``{tenant}-INC-000012`` yields the incident id. Anything else is ignored."""

    prefix = f"{tenant_id}-"
    if not name.startswith(prefix):
        return None
    incident_id = name[len(prefix) :]
    if _INCIDENT_NAME.fullmatch(incident_id) is None:
        return None
    return incident_id


def newest_parent(stepfunctions: Any, cold_chain_arn: str, tenant_id: str) -> Execution:
    """The newest cold-chain execution whose name belongs to this tenant."""

    found = _for_tenant(_list_executions(stepfunctions, cold_chain_arn), tenant_id)
    if not found:
        raise DemoError(f"no cold-chain execution for {tenant_id}")
    return found[0]


def execution_path(stepfunctions: Any, execution_arn: str) -> list[str]:
    """State names in the order they were entered."""

    names: list[str] = []
    for event in _history(stepfunctions, execution_arn):
        details = event.get("stateEnteredEventDetails")
        if not isinstance(details, dict):
            continue
        name = details.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def child_for(
    stepfunctions: Any,
    engage_arn: str,
    tenant_id: str,
    incident_id: str,
) -> tuple[str, str] | None:
    """``(status, current state)`` for this incident's engage-party execution."""

    for execution in _list_executions(stepfunctions, engage_arn)[:50]:
        described_tenant, described_incident = _execution_ids(stepfunctions, execution.arn)
        if described_tenant != tenant_id or described_incident != incident_id:
            continue
        path = execution_path(stepfunctions, execution.arn)
        return execution.status, path[-1] if path else ""
    return None


def incident_state(dynamodb: Any, table: str, tenant_id: str, incident_id: str) -> str:
    """The META state of one tenant incident."""

    item = dynamodb.get_item(
        TableName=table,
        Key=key(incident_pk(tenant_id, incident_id), "META"),
    ).get("Item")
    state = _text(item, "state")
    if not state:
        raise DemoError(f"incident {incident_id} has no state")
    return state


def router_is_live(lambda_client: Any) -> bool:
    """True when the deployed router has WhatsApp mock mode off."""

    try:
        configuration = lambda_client.get_function_configuration(FunctionName=ROUTER_FUNCTION)
    except Exception:
        raise DemoError(f"cannot read USE_MOCK_WHATSAPP on {ROUTER_FUNCTION}") from None
    variables = None
    if isinstance(configuration, dict):
        variables = configuration.get("Environment", {}).get("Variables")
    try:
        return channels_live_from_configuration(variables)
    except SystemExit as exc:
        message = exc.code if isinstance(exc.code, str) else ""
        raise DemoError(message or f"cannot read USE_MOCK_WHATSAPP on {ROUTER_FUNCTION}") from None


def send_driver_reply(profile: str, tenant_id: str, shipment_id: str, button: str) -> None:
    """Post the driver button through ``scripts.demo_reply``."""

    try:
        post_reply(
            [
                "--profile",
                profile,
                "--tenant",
                tenant_id,
                "--shipment",
                shipment_id,
                "--role",
                "driver",
                "--button",
                button,
            ]
        )
    except SystemExit as exc:
        if exc.code in (0, None):
            return
        message = exc.code if isinstance(exc.code, str) else "driver reply failed"
        raise DemoError(message) from None


def poll(clock: Clock, sleep: Sleeper, timeout: float, probe: Callable[[], Any]) -> Any:
    """Call ``probe`` until it returns a truthy value or the deadline passes."""

    deadline = clock() + timedelta(seconds=timeout)
    while True:
        found = probe()
        if found:
            return found
        now = clock()
        if now >= deadline:
            return None
        sleep(min(POLL_SECONDS, (deadline - now).total_seconds()))


def say(line: str) -> None:
    """Print one operator line. A line that would leak a secret is refused."""

    if _leaks(line):
        raise DemoError("refusing to print a secret")
    print(line, flush=True)


def public_error(exc: BaseException) -> str:
    """An error line with no ARN, number, token, or raw payload."""

    if isinstance(exc, DemoError):
        text = str(exc).strip() or "demo command failed"
    else:
        raw = str(exc).strip()
        detail = raw.splitlines()[0].strip() if raw else ""
        if detail and not _leaks(detail):
            text = detail
        else:
            text = f"demo command failed ({type(exc).__name__})"
    if _leaks(text):
        return "demo command failed"
    return text[:300]


def _prepare_session(session: Any, tenant_id: str) -> None:
    prepare(_resources(session), tenant_id)


def _cleanup_session(session: Any, tenant_id: str) -> None:
    cleanup(_resources(session), tenant_id)


def _require_clear(counts: Mapping[str, int], prefix: str) -> None:
    leftover = {name: count for name, count in counts.items() if count}
    if not leftover:
        return
    summary = " ".join(f"{name}={count}" for name, count in leftover.items())
    raise DemoError(f"{prefix} {summary}")


def _prompt_ready(clients: Any, tenant_id: str, incident_id: str) -> bool:
    child = child_for(clients.stepfunctions, clients.engage_party_arn, tenant_id, incident_id)
    return bool(child and child[0] == "RUNNING" and child[1] == PROMPT_STATE)


def _new_execution(
    stepfunctions: Any,
    cold_chain_arn: str,
    tenant_id: str,
    known: set[str],
) -> Execution | None:
    for execution in _for_tenant(_list_executions(stepfunctions, cold_chain_arn), tenant_id):
        if execution.name not in known:
            return execution
    return None


def _for_tenant(executions: list[Execution], tenant_id: str) -> list[Execution]:
    prefix = f"{tenant_id}-INC-"
    return [item for item in executions if item.name.startswith(prefix)]


def _list_executions(stepfunctions: Any, machine_arn: str) -> list[Execution]:
    found: list[Execution] = []
    token: str | None = None
    while True:
        request: dict[str, Any] = {"stateMachineArn": machine_arn, "maxResults": 100}
        if token:
            request["nextToken"] = token
        try:
            page = stepfunctions.list_executions(**request)
        except Exception:
            raise DemoError("could not list executions") from None
        if not isinstance(page, dict):
            raise DemoError("could not list executions")
        for item in page.get("executions") or []:
            if isinstance(item, dict):
                execution = _execution(item)
                if execution is not None:
                    found.append(execution)
        token = page.get("nextToken")
        if not isinstance(token, str) or not token:
            return found


def _history(stepfunctions: Any, execution_arn: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        request: dict[str, Any] = {
            "executionArn": execution_arn,
            "reverseOrder": False,
            "maxResults": 1000,
        }
        if token:
            request["nextToken"] = token
        try:
            page = stepfunctions.get_execution_history(**request)
        except Exception:
            raise DemoError("could not read the execution") from None
        if not isinstance(page, dict):
            raise DemoError("could not read the execution")
        events.extend(item for item in page.get("events") or [] if isinstance(item, dict))
        token = page.get("nextToken")
        if not isinstance(token, str) or not token:
            return events


def _execution_ids(stepfunctions: Any, execution_arn: str) -> tuple[str | None, str | None]:
    try:
        described = stepfunctions.describe_execution(executionArn=execution_arn)
    except Exception:
        return None, None
    raw = described.get("input") if isinstance(described, dict) else None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
    elif isinstance(raw, dict):
        parsed = raw
    else:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    tenant = parsed.get("tenant_id")
    incident = parsed.get("incident_id")
    return (
        tenant if isinstance(tenant, str) else None,
        incident if isinstance(incident, str) else None,
    )


def _execution(item: Mapping[str, Any]) -> Execution | None:
    name = item.get("name")
    status = item.get("status")
    arn = item.get("executionArn")
    if not all(isinstance(value, str) and value for value in (name, status, arn)):
        return None
    return Execution(name=name, status=status, arn=arn)


def _range_for(document: Mapping[str, Any], profile_id: str) -> tuple[float, float] | None:
    profiles = document.get("product_profiles")
    if not isinstance(profiles, list):
        return None
    for profile in profiles:
        if not isinstance(profile, dict) or profile.get("id") != profile_id:
            continue
        band = profile.get("range_c")
        if not isinstance(band, list) or len(band) != 2:
            return None
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in band):
            return None
        low, high = float(band[0]), float(band[1])
        if low < high:
            return low, high
        return None
    return None


def _caller_account(session: Any) -> str:
    try:
        identity = _client(session, "sts").get_caller_identity()
    except Exception:
        raise DemoError("could not read the caller account") from None
    account = identity.get("Account") if isinstance(identity, dict) else None
    if not isinstance(account, str) or not account.strip():
        raise DemoError("could not read the caller account")
    return account.strip()


def _resources(session: Any) -> Any:
    try:
        return clients_from_session(session, DATA_STACK)
    except LookupError as exc:
        raise DemoError(f"stack outputs are missing: {exc}") from None
    except Exception:
        raise DemoError(f"could not read stack {DATA_STACK}") from None


def _text(item: Mapping[str, Any] | None, name: str) -> str:
    if not item:
        return ""
    value = item.get(name)
    if not isinstance(value, dict):
        return ""
    raw = value.get("S")
    return raw.strip() if isinstance(raw, str) else ""


def _number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return format(value, "g")


def _leaks(text: str) -> bool:
    return _LEAK.search(text) is not None


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""


def _client(session: Any, name: str) -> Any:
    return session.client(name, region_name=REGION)


def _session(profile: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name=REGION)


def _now() -> datetime:
    return datetime.now(UTC)


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", required=True)
    common.add_argument("--account", required=True, help="12-digit account id the caller must match.")
    common.add_argument("--tenant", required=True)
    common.add_argument("--shipment", default=None)
    common.add_argument("--device", default=None)
    common.add_argument(
        "--live",
        action="store_true",
        help="Live channels. Preflight checks the driver parameter. Reply is refused.",
    )
    parser = argparse.ArgumentParser(
        prog="scripts.demo",
        description="Operator demo for one tenant. Not part of CI.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", parents=[common], help="Check the demo account before a run.")
    commands.add_parser("prep", parents=[common], help="Reset, reseed, and write runtime rows.")
    sensor = commands.add_parser("sensor", parents=[common], help="Publish the excursion readings.")
    sensor.add_argument("--temps", default="8.1,8.6")
    sensor.add_argument("--pause", type=float, default=4)
    reply = commands.add_parser("reply", parents=[common], help="Post the mocked driver reply.")
    reply.add_argument("--button", default="running", choices=("running", "stopped"))
    commands.add_parser("status", parents=[common], help="Print the execution and incident.")
    commands.add_parser("cleanup", parents=[common], help="Reset the tenant and require zero.")
    run = commands.add_parser("run", parents=[common], help="Prep, sensor, mocked reply, status.")
    run.add_argument("--temps", default="8.1,8.6")
    run.add_argument("--pause", type=float, default=4)
    run.add_argument("--button", default="running", choices=("running", "stopped"))
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
