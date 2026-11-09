# Bedrock access (Nova Lite)

Vision and the AgentCore runtime use Amazon Nova Lite in us-east-1 through the Bedrock Runtime Converse API ([ADR-020](adr/ADR-020-bedrock-model.md)). The model id is CDK context `bedrock:modelId`, default `amazon.nova-lite-v1:0`. There is no fallback model in code.

This page is the operator runbook (sheet G2). It does not grant console access. The account owner enables the model in Bedrock. Live InvokeModel happens only when channels are live.

## When the model is called

| Path | Mock | Live |
| --- | --- | --- |
| Evidence photos (`oerf/platform/evidence/vision.py`) | `USE_MOCK_BEDROCK` defaults to `true` | `channels:live=true` turns the mock off and calls Converse |
| AgentCore free-text advice | Runtime is not in the template | `agentcore:enabled=true` builds the ARM64 runtime; Interpret still keeps the rule result |

Guardrail apply stays OERF-310. The vision and runtime roles may `bedrock:InvokeModel` on the Nova Lite foundation-model ARN and `bedrock:ApplyGuardrail` on the account guardrail plus the `us.guardrail.v1:0` profile in us-east-1, us-east-2, and us-west-2.

## Account setup (once)

1. In the Bedrock console in **us-east-1**, enable **Amazon Nova Lite** on-demand for this account.
2. Confirm the guardrail id used as `dev:guardrailArn` (dev) or the main guardrail. Dev synth of `OerfCiStack-dev` requires `arn:aws:bedrock:us-east-1:<account>:guardrail/<id>`.
3. Do not put model keys in git. Bedrock IAM is the credential.

Changing the model to Claude Haiku 4.5 means updating `bedrock:modelId` **and** the `InvokeModel` resource to the US inference profile `us.anthropic.claude-haiku-4-5-20251001-v1:0` in the same change.

## Dev vs main

Dev physical names use the `oerf-dev-` prefix. The same model and region apply. `channels:live` on a GitHub **dev** deploy is the environment variable `CHANNELS_LIVE` (false unless set). Leave it false until WhatsApp IAM is scoped with `whatsapp:phoneNumberId`.

## After the first dev deploy (A6)

1. With `channels:live` still false, an evidence photo must not call Bedrock (mock path).
2. With `channels:live` true and the model enabled, a driver photo should reach Nova Lite and show evidence on the desk (C10). Until that smoke runs, treat this runbook as setup only.
3. Do not log prompt text, image bytes, or phone numbers.

Channel credentials and template checks are in [provider-onboarding.md](provider-onboarding.md). The runtime itself is [agentcore.md](agentcore.md).
