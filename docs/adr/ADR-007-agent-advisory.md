# ADR-007: Rules decide, the agent advises

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

Amazon Bedrock AgentCore runs the advisory. The runtime is an ARM64 container, created only when context `agentcore:enabled` is true. The default is false. The model is Amazon Nova Lite ([ADR-020](ADR-020-bedrock-model.md)), through a Strands agent.

The agent may read an incident and the tenant config and return an advisory. It cannot send a message, write incident state, or hold a task token. Interpret classifies the reply with rules. Buttons and known keywords never call the agent. Free text may call it, and only when the flag is on. Interpret stores `status` and `fallback` from that response and drops any recommended action.

## Reasons

The principle is that AI understands, rules decide, and people approve. A model that can send or change state would make the audit trail depend on a prompt.

## Consequences

With the flag off, Interpret stays on the rule result. A missing runtime, an error, or an unusable body becomes the fallback `{status: placeholder, fallback: true}`, and the rule result stands.

The execution role can read the incident and tenant tables, invoke Nova Lite, and apply the guardrail. It cannot write those tables or call Step Functions, WhatsApp, SMS, or email.

An early note said the agent drafts the outbound message. The deployed router renders approved templates. The agent does not.
