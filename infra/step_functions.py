"""Fixed names for the six cold-chain step functions.

``OerfDataStack`` writes these names into ``oerf-cold-chain`` as literal Lambda
ARNs. It does not reference ``OerfComputeStack``. ``OerfComputeStack`` creates
the functions with the same names.
"""

from __future__ import annotations

#: Phase name to the deployed function name.
STEP_FUNCTION_NAMES = {
    "context": "oerf-step-context",
    "assess": "oerf-step-assess",
    "act": "oerf-step-act",
    "verify": "oerf-step-verify",
    "qa": "oerf-step-qa",
    "close": "oerf-step-close",
}

#: Phase name to the frozen handler path in ``docs/interfaces.md`` section 8.
STEP_HANDLERS = {
    "context": "domains.cold_chain_excursion.steps.context.handler",
    "assess": "domains.cold_chain_excursion.steps.assess.handler",
    "act": "domains.cold_chain_excursion.steps.act.handler",
    "verify": "domains.cold_chain_excursion.steps.verify.handler",
    "qa": "domains.cold_chain_excursion.steps.qa.handler",
    "close": "domains.cold_chain_excursion.steps.close.handler",
}

#: ASL placeholder to the phase whose function replaces it.
ASL_STEP_PLACEHOLDERS = {
    "__STEP_CONTEXT_ARN__": "context",
    "__STEP_ASSESS_ARN__": "assess",
    "__STEP_ACT_ARN__": "act",
    "__STEP_VERIFY_ARN__": "verify",
    "__STEP_QA_ARN__": "qa",
    "__STEP_CLOSE_ARN__": "close",
}

#: Cold-chain state name to the phase that serves it. ``VerifyLoop`` calls the
#: verify handler; its payload step name stays ``Verify``.
WIRED_STATES = {
    "GatherContext": "context",
    "Assess": "assess",
    "Act": "act",
    "VerifyLoop": "verify",
    "QA": "qa",
    "Close": "close",
}


def function_arn(region: str, account: str, name: str) -> str:
    """Unqualified Lambda ARN for a fixed function name."""

    return f"arn:aws:lambda:{region}:{account}:function:{name}"
