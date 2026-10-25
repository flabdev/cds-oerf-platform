"""Fixed names for the cold-chain step functions.

``OerfDataStack`` writes these names into ``oerf-cold-chain`` as literal Lambda
ARNs. It does not reference ``OerfComputeStack``. ``OerfComputeStack`` creates
the functions with the same names.
"""

from __future__ import annotations

#: Phase name to the deployed function name.
STEP_FUNCTION_NAMES = {
    "context": "oerf-step-context",
    "diagnose": "oerf-step-diagnose",
    "assess": "oerf-step-assess",
    "act": "oerf-step-act",
    "verify": "oerf-step-verify",
    "qa": "oerf-step-qa",
    "close": "oerf-step-close",
    "record_failure": "oerf-step-record-failure",
    "escalate": "oerf-step-escalate",
    "cancel": "oerf-step-cancel",
}

#: Engagement task to the deployed function name. ``SendViaRouter`` invokes the
#: existing channel router; it does not get a function of its own.
ENGAGE_FUNCTION_NAMES = {
    "cadence": "oerf-engage-cadence",
    "interpret": "oerf-engage-interpret",
}

CHANNEL_ROUTER_NAME = "oerf-channel-router"

#: Phase name to the frozen handler path in ``docs/interfaces.md`` section 8.
STEP_HANDLERS = {
    "context": "domains.cold_chain_excursion.steps.context.handler",
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
    "__STEP_CONTEXT_ARN__": "context",
    "__STEP_DIAGNOSE_ARN__": "diagnose",
    "__STEP_ASSESS_ARN__": "assess",
    "__STEP_ACT_ARN__": "act",
    "__STEP_VERIFY_ARN__": "verify",
    "__STEP_QA_ARN__": "qa",
    "__STEP_CLOSE_ARN__": "close",
    "__STEP_RECORD_FAILURE_ARN__": "record_failure",
    "__STEP_ESCALATE_ARN__": "escalate",
    "__STEP_CANCEL_ARN__": "cancel",
}

ASL_ENGAGE_PLACEHOLDERS = {
    "__ENGAGE_CADENCE_ARN__": "cadence",
    "__ENGAGE_INTERPRET_ARN__": "interpret",
}

#: Cold-chain state name to the phase that serves it. ``VerifyLoop`` calls the
#: verify handler; its payload step name stays ``Verify``.
WIRED_STATES = {
    "GatherContext": "context",
    "Diagnose": "diagnose",
    "Assess": "assess",
    "Act": "act",
    "VerifyLoop": "verify",
    "QA": "qa",
    "Close": "close",
    "RecordFailure": "record_failure",
    "Escalate": "escalate",
    "Cancel": "cancel",
}


def function_arn(region: str, account: str, name: str) -> str:
    """Unqualified Lambda ARN for a fixed function name."""

    return f"arn:aws:lambda:{region}:{account}:function:{name}"
