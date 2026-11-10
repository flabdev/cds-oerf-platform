"""Fixed names for the cold-chain step functions.

``OerfDataStack`` writes these names into the cold-chain state machine as
literal Lambda ARNs. It does not reference ``OerfComputeStack``.
``OerfComputeStack`` creates the functions with the same names.

The maps below are the main-stage names. ``names_for`` applies a ``Stage``.
"""

from __future__ import annotations

from infra.naming import Stage

#: Phase name to the deployed function name.
STEP_FUNCTION_NAMES = {
    "open": "oerf-step-open",
    "context": "oerf-step-context",
    "diagnose": "oerf-step-diagnose",
    "assess": "oerf-step-assess",
    "approval": "oerf-step-approval",
    "secure_facility": "oerf-step-secure-facility",
    "act": "oerf-step-act",
    "verify": "oerf-step-verify",
    "record_containment": "oerf-step-record-containment",
    "begin_disposition": "oerf-step-begin-disposition",
    "handle_reopen": "oerf-step-handle-reopen",
    "generate_report": "oerf-step-generate-report",
    "qa": "oerf-step-qa",
    "close": "oerf-step-close",
    "no_response": "oerf-step-no-response",
    "record_failure": "oerf-step-record-failure",
    "escalate": "oerf-step-escalate",
    "cancel": "oerf-step-cancel",
}

#: Engagement task to the deployed function name. ``SendViaRouter`` invokes the
#: existing channel router; it does not get a function of its own.
ENGAGE_FUNCTION_NAMES = {
    "cadence": "oerf-engage-cadence",
    "interpret": "oerf-engage-interpret",
    "lifecycle": "oerf-engage-lifecycle",
}

CHANNEL_ROUTER_NAME = "oerf-channel-router"


def _apply(stage: Stage, main_name: str) -> str:
    """``oerf-step-context`` stays that on main and becomes ``oerf-dev-step-context``."""

    prefix = "oerf-"
    if not main_name.startswith(prefix):
        raise ValueError(f"expected an oerf- name, got {main_name!r}")
    return stage.res(main_name[len(prefix) :])


def step_function_names_for(stage: Stage) -> dict[str, str]:
    return {key: _apply(stage, name) for key, name in STEP_FUNCTION_NAMES.items()}


def engage_function_names_for(stage: Stage) -> dict[str, str]:
    return {key: _apply(stage, name) for key, name in ENGAGE_FUNCTION_NAMES.items()}


def channel_router_name_for(stage: Stage) -> str:
    return _apply(stage, CHANNEL_ROUTER_NAME)

#: Phase name to the frozen handler path in ``docs/interfaces.md`` section 8.
STEP_HANDLERS = {
    "open": "domains.cold_chain_excursion.steps.open.handler",
    "context": "domains.cold_chain_excursion.steps.context.handler",
    "approval": "domains.cold_chain_excursion.steps.approval.handler",
    "secure_facility": "domains.cold_chain_excursion.steps.secure_facility.handler",
    "record_containment": "domains.cold_chain_excursion.steps.record_containment.handler",
    "begin_disposition": "domains.cold_chain_excursion.steps.begin_disposition.handler",
    "handle_reopen": "domains.cold_chain_excursion.steps.handle_reopen.handler",
    "generate_report": "domains.cold_chain_excursion.steps.generate_report.handler",
    "no_response": "domains.cold_chain_excursion.steps.no_response.handler",
    "diagnose": "domains.cold_chain_excursion.steps.diagnose.handler",
    "assess": "domains.cold_chain_excursion.steps.assess.handler",
    "act": "domains.cold_chain_excursion.steps.act.handler",
    "verify": "domains.cold_chain_excursion.steps.verify.handler",
    "qa": "domains.cold_chain_excursion.steps.qa.handler",
    "close": "domains.cold_chain_excursion.steps.close.handler",
    "record_failure": "domains.cold_chain_excursion.steps.record_failure.handler",
    "escalate": "domains.cold_chain_excursion.steps.escalate.handler",
    "cancel": "domains.cold_chain_excursion.steps.cancel.handler",
}

#: ASL placeholder to the phase whose function replaces it.
ASL_STEP_PLACEHOLDERS = {
    "__STEP_OPEN_ARN__": "open",
    "__STEP_CONTEXT_ARN__": "context",
    "__STEP_DIAGNOSE_ARN__": "diagnose",
    "__STEP_ASSESS_ARN__": "assess",
    "__STEP_APPROVAL_ARN__": "approval",
    "__STEP_SECURE_FACILITY_ARN__": "secure_facility",
    "__STEP_ACT_ARN__": "act",
    "__STEP_VERIFY_ARN__": "verify",
    "__STEP_RECORD_CONTAINMENT_ARN__": "record_containment",
    "__STEP_BEGIN_DISPOSITION_ARN__": "begin_disposition",
    "__STEP_HANDLE_REOPEN_ARN__": "handle_reopen",
    "__STEP_GENERATE_REPORT_ARN__": "generate_report",
    "__STEP_QA_ARN__": "qa",
    "__STEP_CLOSE_ARN__": "close",
    "__STEP_NO_RESPONSE_ARN__": "no_response",
    "__STEP_RECORD_FAILURE_ARN__": "record_failure",
    "__STEP_ESCALATE_ARN__": "escalate",
    "__STEP_CANCEL_ARN__": "cancel",
}

ASL_ENGAGE_PLACEHOLDERS = {
    "__ENGAGE_CADENCE_ARN__": "cadence",
    "__ENGAGE_INTERPRET_ARN__": "interpret",
    "__ENGAGE_LIFECYCLE_ARN__": "lifecycle",
}

#: Cold-chain state name to the phase that serves it. ``VerifyLoop`` calls the
#: verify handler; its payload step name stays ``Verify``.
WIRED_STATES = {
    "OPEN": "open",
    "GatherContext": "context",
    "Diagnose": "diagnose",
    "Assess": "assess",
    "Approval": "approval",
    "SecureFacility": "secure_facility",
    "Act": "act",
    "VerifyLoop": "verify",
    "RecordContainment": "record_containment",
    "BeginDisposition": "begin_disposition",
    "HandleReopen": "handle_reopen",
    "GenerateReport": "generate_report",
    "QA": "qa",
    "Close": "close",
    "NoResponse": "no_response",
    "RecordFailure": "record_failure",
    "Escalate": "escalate",
    "Cancel": "cancel",
}


def function_arn(region: str, account: str, name: str) -> str:
    """Unqualified Lambda ARN for a fixed function name."""

    return f"arn:aws:lambda:{region}:{account}:function:{name}"
