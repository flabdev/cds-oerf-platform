# AgentCore runtime

Interpret may ask Bedrock AgentCore for an advisory on free-text driver replies. The runtime does not choose the intent, write the incident, send a channel message, or hold a task token ([ADR-007](adr/ADR-007-agent-advisory.md), interfaces § AgentCore).

This page is the operator runbook (sheet G2). Image build and IAM are OERF-312. Live invocation smoke is C11 and waits on the first **dev** deploy (A6).

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
`response.intent` and `response.valid`. The confidence floor is `0.8`
(`CLASSIFIER_CONFIDENCE_FLOOR`).

The classifier is `oerf/platform/agent/agentcore/classifier.py`. The schema is
closed (Pydantic, `extra="forbid"`, `Literal` enums, allow-listed fields), the
prompt marks the reply as untrusted data, and the existing Bedrock Guardrail
applies. The model call is traced with the allow-listed fields only; the reply
text, the prompt, and the raw model output are never logged or traced.

Fault codes stay unverified. The runtime drops a proposed code unless the
tenant's own `FAULTCODE#` catalog (pattern 5) contains it, and the Interpret
caller fails closed unconditionally because its role has no tenant-table grant
(OERF-211 does not widen runtime IAM). No seed file or seed run writes
`FAULTCODE#` rows yet, so E17/E05/E22/E09 cannot be accepted until the
authoritative per-tenant catalog source and its dev/test provisioning are added.
There is no global allow-list.

Rollback is a deploy with `agentcore:enabled` false. That removes the runtime from the compute stack. Do not delete `AWS::XRay::TransactionSearchConfig` on a whim: it is account-wide and is created on the data stack when AgentCore is on ([runbook-deploy.md](runbook-deploy.md)).

## After the first dev deploy (A6)

1. Confirm `OerfComputeStack-dev` is `*_COMPLETE` and output `AgentRuntimeArn` is set.
2. Synth with `-c stage=dev -c agentcore:enabled=true` must stay green in CI (already a required job).
3. A live free-text reply smoke (C11) is not claimed here.

Model enablement is [bedrock-access.md](bedrock-access.md).
