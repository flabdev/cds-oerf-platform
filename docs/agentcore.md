# AgentCore runtime

Interpret may ask Bedrock AgentCore for an advisory on free-text driver replies. The runtime does not choose the intent, write the incident, send a channel message, or hold a task token ([ADR-007](adr/ADR-007-agent-advisory.md), interfaces § AgentCore).

This page is the operator runbook (sheet G2). To use the Jev decision model as the free-text proposer instead, see [jev.md](jev.md). Image build and IAM are OERF-312. Live invocation smoke is C11 and waits on the first **dev** deploy (A6).

## Adopted approach note

[AgentCore implementation approach](agentcore-implementation-approach.md) is retained as a planning reference under OERF-215. Its source is preserved verbatim with provenance and a reconciliation note: it predates the accepted OERF-209/ADR-042 free-text contract and is not authoritative where it conflicts with current behavior. Current acceptance status and operator guidance remain on this page and in [ADR-042](adr/ADR-042-driver-reply-classification.md).

## What deploys

Context `agentcore:enabled` defaults to **false** so `cdk synth` does not build an image. When it is true:

- `OerfComputeStack` adds the runtime (`aws_cdk.aws_bedrockagentcore.Runtime`).
- The image context is `oerf/platform/agent/agentcore/`. The repo root is the Docker build context.
- Architecture is ARM64. Idle timeout is 60 seconds. Max lifetime is 5 minutes.
- Runtime name is `oerf_agent` on main and `oerf_dev_agent` on dev. Logs go to `/aws/vendedlogs/bedrock-agentcore/<runtime-name>`.
- The execution role may read the incident and tenant tables, `bedrock:InvokeModel` on Nova Lite, and `bedrock:ApplyGuardrail` on the guardrail ARNs in [iam-exceptions.md](security/iam-exceptions.md).
- Stack output `AgentRuntimeArn` (export `AGENT-RUNTIME-ARN` or `AGENT-RUNTIME-ARN-dev`) is passed into Interpret as `AGENT_RUNTIME_ARN`.

`oerf/platform/agent/runtime.py` stays the step composition root. The container is not that module.

Dev laptop and GitHub deploys currently pass `-c agentcore:enabled=true` (see [infra/bootstrap/README.md](../infra/bootstrap/README.md) and `.github/workflows/deploy.yml`). A Markdown-only push to `develop` does not deploy.

## Behaviour

1. Cadence and the router send the check-in.
2. Inbound resumes the wait token.
3. Interpret maps buttons and keywords itself.
4. Free text may invoke the runtime. The advisory is attached; the rule result stands.
5. If the runtime is unset, times out, or errors, Interpret continues without the model.

### Free-text driver classification (`task=interpret`, OERF-211)

A free-text driver reply that matches no button or keyword may be offered to the
runtime with `task=interpret` and `inputs.reply_text` only (the 4,000-character
inbound cap). The runtime returns a bounded proposal and never a `valid` flag:

```json
{"status": "proposal", "fallback": false,
 "proposal": {"category": "answer", "intent": "unit_running", "fields": {}, "confidence": 0.9}}
```

Anything unusable — a missing or oversized reply, a guardrail block, a schema
violation, a model error, or a proposed fault code the tenant catalog does not
know — returns the documented fallback `{"status": "placeholder", "fallback": true}`,
and Interpret maps it to `clarify`. The proposal is validated by
`oerf.platform.cadence.interpret.validate_proposal`, which owns the final
`response.intent` and `response.valid`. The Interpret Lambda uses the tenant's
`evidence.min_confidence`. The packaged default is `0.8`
(`CLASSIFIER_CONFIDENCE_FLOOR`). A missing, unreadable, or out-of-range floor
fails closed to `clarify` and does not fall back to a lower number. Callers
that pass no tenant policy, including the offline evaluator, still use `0.8`.

The classifier is `oerf/platform/agent/agentcore/classifier.py`. The schema is
closed (Pydantic, `extra="forbid"`, `Literal` enums, allow-listed fields), the
prompt marks the reply as untrusted data, and the existing Bedrock Guardrail
applies. The model call is traced with the allow-listed fields only; the reply
text, the prompt, and the raw model output are never logged or traced.

Fault codes stay unverified until that tenant's own `FAULTCODE#` catalog
contains them. The row shape is `TENANT#{tenant_id}` / `FAULTCODE#{code}`
(pattern 5). The runtime and the Interpret Lambda both read that tenant's rows
and fail closed when the catalog is missing or unreadable. Tenant t1's seed
writes a synthetic demo catalog for E17, E05, E22, and E09
(`sim/seed/t1/faultcodes.json`). It is not an authoritative catalog. Other
tenants still have no rows, so a code proposal for them fails closed. There is
no global allow-list.

### Timeouts

The Interpret Lambda and the `Interpret` state both time out at 30 seconds. A
slow model call must return before that kill so Interpret can map it to
`clarify` instead of `EngagementFailed`.

| Client | Connect | Read | Attempts | Worst case |
| --- | --- | --- | --- | --- |
| Interpret tenant-table read | 1s | 2s | 1 | 3s |
| Interpret `bedrock-agentcore` | 1s | 10s | 2 | 22s |
| Classifier Bedrock | 1s | 6s | 1 | 7s |

The table read and the AgentCore call run one after the other, so their combined worst case is 25 seconds. The Bedrock call happens inside one AgentCore read.

Environment variables `AGENTCORE_CONNECT_TIMEOUT`, `AGENTCORE_READ_TIMEOUT`,
and `AGENTCORE_MAX_ATTEMPTS` tune the caller. `CLASSIFIER_CONNECT_TIMEOUT`,
`CLASSIFIER_READ_TIMEOUT`, and `CLASSIFIER_MAX_ATTEMPTS` tune the runtime.
Values outside the ranges above are ignored and the defaults are used, so an
override cannot grow past this budget. The runtime idle timeout stays 60
seconds and the max lifetime stays 5 minutes.

## OERF-214 dev acceptance and operator checklist

### Configuration and enablement

The supported deployment switch is CDK context `agentcore:enabled`; default is `false`. The compute stack sets `AGENTCORE_ENABLED=false` and does not set `AGENT_RUNTIME_ARN` when disabled. When enabled, it sets `AGENTCORE_ENABLED=true`, wires the `AgentRuntimeArn` stack output to `AGENT_RUNTIME_ARN`, and grants the Interpret caller `bedrock-agentcore:InvokeAgentRuntime` only on that runtime ARN. Interpret also has read-only access to the tenant table so it can resolve `evidence.min_confidence` and `FAULTCODE#` rows. It cannot write that table, send a message, or resume a task token. Check these behaviors in `infra/stacks/compute_stack.py` before changing deployment wiring.

Deploy/inspect dev using the existing operator procedures in [infra/bootstrap/README.md](../infra/bootstrap/README.md) and [runbook-deploy.md](runbook-deploy.md). Before a real model rehearsal, confirm `OerfComputeStack-dev` is `*_COMPLETE`, its `AgentRuntimeArn` output is present, and the deployed Interpret configuration has `AGENTCORE_ENABLED=true` plus that ARN. Do not print or paste the ARN into a ticket/report. CI's credential-free enabled synth is `cdk synth --lookups false --quiet -c account=000000000000 -c stage=dev -c agentcore:enabled=true`; the default/off synth must remain deterministic and must not build the runtime image.

Known buttons and policy-scoped keywords are resolved before the optional classifier and must remain deterministic with AgentCore on or off. With the flag disabled, a missing ARN, or an invocation error/timeout, unmatched free text maps to invalid `clarify` and the existing `ResetLadder`; it is not consumed. The existing exact-match button/keyword mapping remains available. Do not turn an offline fake score into a Bedrock/model score.

### Credential-free acceptance rehearsal (2026-11-18)

This rehearsal used synthetic proposals through the production `interpret_workflow` validation boundary. It made no AWS calls, used no live model, and contains no driver reply text or PII. The table records expected corpus category, validated intent, validity, and terminal ASL route:

| Synthetic case | Expected category | Validated intent | Valid | Terminal path |
| --- | --- | --- | --- | --- |
| Running status | `answer` | `unit_running` | yes | `AcknowledgeReply` → `Answered` |
| Stopped status | `answer` | `unit_stopped` | yes | `AcknowledgeReply` → `Answered` |
| Unknown status | `answer` | `unknown` | yes | `AcknowledgeReply` → `Answered` |
| Decline | `decline` | `decline` | yes | `AcknowledgeReply` → `Answered` |
| Callback request | `call-me` | `call_me` | yes | `HandleCallback` (not consumed as an answer) |
| Help request | `help` | `clarify` | no | `ResetLadder` (not consumed) |
| Safety concern | `concern` | `clarify` | no | `ResetLadder` (not consumed) |
| Unrelated message | `unrelated` | `clarify` | no | `ResetLadder` (not consumed) |
| Ambiguous / below confidence floor | `answer` proposal rejected | `clarify` | no | `ResetLadder` (not consumed) |
| Runtime unavailable / exception | no usable proposal | `clarify` | no | `ResetLadder` (not consumed) |

The synthetic table validates deterministic mapping and route wiring, not model quality. The separate 42-row offline evaluator is also a fixture/evaluator contract check only. Tenant t1 now has a seeded synthetic catalog for E17, E05, E22, and E09; it is demo data, not an authoritative catalog. Other tenants still fail closed. The offline evaluator keeps its own fixture catalog and does not read that seed.

### Defect found and fixed during rehearsal (2026-11-18)

The defect was found while investigating why free-text replies were falling back to `clarify`. A local reproduction using the real `strands-agents` 1.65.0 SDK result types confirmed the failure. This is not evidence of a new live dev invocation; deployment and live re-rehearsal remain outstanding. The cause was a runtime trace-shape defect, not the model, the guardrail, or the tenant catalog:

* `classify_reply` (and `advisory.note_for`) read `AgentResult.metrics` as a mapping and called `metrics.get("stop_reason")`. The `strands` SDK sets `AgentResult.metrics` to an `EventLoopMetrics` **dataclass** whose token totals live on `accumulated_usage`; it has no `.get`.
* That raised `AttributeError` inside the guarded call. The `except Exception` block recorded `outcome="ERROR"` and returned no proposal, so the runtime returned the documented fallback and Interpret mapped every reply to `clarify`.
* The unit suite missed it because its fake agent returned dict-shaped `metrics`, a shape the real SDK never produces.

The fix reads both shapes: usage comes from `accumulated_usage` (object) or `usage` (mapping), the stop reason comes from the result's own `stop_reason`, and an unrecognized shape yields an empty trace instead of raising. The proposal is also read before the trace, so a trace-shape surprise can never discard a usable proposal. The failure log now names the exception type only (no reply or model text), which is what made this defect invisible in the dev log group.

Regression coverage (credential-free, no SDK install): `tests/unit/agent/test_agentcore_classifier.py::test_the_real_sdk_metrics_object_still_yields_a_proposal` and `::test_the_trace_reads_tokens_and_stop_reason_from_the_sdk_object`, plus `tests/unit/agent/test_agentcore_decision.py::test_the_real_sdk_metrics_object_still_yields_a_note`. Each fails against the pre-fix code and passes after it.

Fault codes remain unverified for the reason above; the fix does not change that gate. Any live re-rehearsal of the deployed boundary is still required to record measured `model_id`, latency, token counts, and guardrail action; until it runs, those remain **not measured** in this record.

No live dev invocation was performed for this record: the session had no usable AWS identity, so deployment status, live runtime availability, and Bedrock/guardrail behavior could not be confirmed. Consequently, measured model latency/token usage, actual model identifier returned by the deployed runtime, runtime prompt/schema version in the deployed image, and live guardrail action are **not measured** here. The implementation defaults to model id `amazon.nova-lite-v1:0`; this is configuration, not evidence of a live invocation. The runtime has a closed proposal schema and confidence floor `0.8` (interfaces v1.54/v1.55; ADR-042); the prompt has no separately recorded version identifier. Record only sanitized metadata (`model_id`, `latency_ms`, input/output token counts, `guardrail_action`, `outcome`) from the existing trace on an authorized dev rehearsal. Never include reply text, prompt, raw model output, or runtime ARN in acceptance evidence.

### Rollback

To disable the feature, deploy the existing dev stacks with `agentcore:enabled=false` (or omit the context, whose default is false) using the established dev deploy procedure. Confirm the resulting Interpret environment has `AGENTCORE_ENABLED=false` and no `AGENT_RUNTIME_ARN`; unmatched free text must clarify through `ResetLadder`, while buttons and known keywords continue to map deterministically. Do not manually delete the account-wide `AWS::XRay::TransactionSearchConfig`: it is created on the data stack when AgentCore is enabled and is not a runtime-only rollback resource. Do not change IAM as part of rollback without a separately reviewed infrastructure change.

### Live corpus score

`python -m scripts.eval_driver_replies_live --confirm-bedrock-call` scores all
42 rows in `tests/ai-evals/driver_replies.jsonl` with the real model. It is
opt-in: without the flag it makes no AWS call. The report is a live model
score, separate from the offline fake. It prints the model id, prompt version,
predicted category, validity, ASL route, and token or latency counts. It does
not print reply text, prompts, or raw model output. `--fail-under` is a manual
gate. One run is 42 model invocations.

No live baseline has been measured yet. Record the first opted-in score here
when an authorized run finishes. Do not copy the offline fake's perfect score
into that line.

A live dev/model acceptance rehearsal remains pending authorized dev access, a deployed runtime, and authoritative tenant fault-code catalog provisioning. Model enablement is documented in [bedrock-access.md](bedrock-access.md).
