# ADR-020: Bedrock model is Amazon Nova Lite

**Status:** Accepted
**Date:** 2026-10-21
**Decider:** Ananda (architect)

## Decision

Vision and the agent use `amazon.nova-lite-v1:0` in us-east-1 through the Bedrock Runtime Converse API. The model id is context `bedrock:modelId`. There is no fallback model in code.

If the output quality is not good enough, change that context value to Claude Haiku 4.5. On `bedrock-runtime` the id is the US inference profile `us.anthropic.claude-haiku-4-5-20251001-v1:0`. That profile is not in-region on-demand, so the vision role's `bedrock:InvokeModel` resource has to be updated in the same change.

## Reasons

Nova Lite is the cheapest multimodal model on Bedrock in us-east-1. The previous vision ids, `anthropic.claude-3-5-sonnet-20241022-v2:0` and `anthropic.claude-3-haiku-20240307-v1:0`, are end of life in us-east-1.

## Consequences

`USE_MOCK_BEDROCK` stays `true` unless context `channels:live` is true. Guardrail configuration remains OERF-310. The agent runtime is still OERF-312; it uses this same model id when it is wired.
