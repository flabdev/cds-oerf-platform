# OERF-206 — AgentCore/Strands decision support, guardrails and evaluation suite

**Issue type:** Story · **Sprint:** B · **Estimate:** 1.5 days


**Depends on:** OERF-202; ADR-020 and Ananda's OERF-304/301 runtime decisions.

### Description
Add bounded AI assistance for free-text diagnosis/response interpretation with Bedrock Guardrails and a deterministic validator. The agent may extract or summarize facts, but cannot override deterministic safety/rule decisions or directly transition incidents. Evaluate against the 42-reply corpus and report measured performance; do not claim a target without test evidence.

### Inputs/outputs
Input tenant-scoped ask, role, message text and explicit allowed facts; output schema-constrained diagnosis/fields, confidence, safety flags, model metadata and citations/provenance as supported. Reject unsupported/unsafe/low-confidence outputs for human review.

### Implementation steps
1. Close ADR-020 model/provider selection; implement a provider abstraction and offline fake for all unit/CI tests.
2. Define strict structured output schema, allowed context and prompt version. Do not pass secrets or unnecessary PII.
3. Add Bedrock Guardrails and deterministic number-match/fault-code validators; deterministic rules remain authoritative.
4. Support misspellings/Hinglish only via explicit evaluation examples; uncertain extraction routes to clarification, never guessed action.
5. Own `tests/ai-evals/driver_replies.jsonl` (at least 42 privacy-safe synthetic replies) and the drafting eval dataset; document expected values, pass/fail criteria and aggregate metric. Mudita/OERF-118 owns the separate photo fixture set in `tests/ai-evals/photos/`. Build a reproducible evaluation runner over the reply/drafting sets.
6. Add unit tests for schema violation, prompt injection, wrong number/fault code, low confidence and fake model behavior; isolate credential-dependent AWS tests.
7. Log model/version/prompt/rule provenance without sensitive message content.

### Acceptance criteria
- [ ] Model/provider choice and fallback behavior documented.
- [ ] Guardrails plus deterministic validators enforce safety boundaries.
- [ ] AI cannot directly perform workflow transitions or override rules.
- [ ] Project lead owns the reply and drafting datasets; OERF-118 owns photo fixtures; all datasets have documented schemas and expected outcomes.
- [ ] 42-reply eval runs reproducibly; measured score reported and target >=95% only if achieved.
- [ ] CI/unit tests require no AWS credentials; privacy/tenant tests pass.

### Implementation boundary and blockers

- `TODO(OERF-312)`: `AGENT_RUNTIME_ARN` is unset and there is no AgentCore runtime entry point in the tree, so the Interpret state has nothing to invoke. The agent half of this ticket cannot start until the ARN and the invoke permission exist.
- `TODO(OERF-310)`: `GUARDRAIL_ID` and `GUARDRAIL_VERSION` are unset and no Bedrock call passes `guardrailConfig`. The guardrail half of this ticket and the guardrail cases in the evaluation suite depend on that wiring.
- The model half is not blocked: ADR-020 is accepted, and the repository already uses `amazon.nova-lite-v1:0` through the Converse API with no fallback model in code.
- `TODO(OERF-116)`: a free-text reply without a quoted message id cannot be correlated yet, so diagnosis input that arrives that way is acknowledged without a callback. The `ADDR#` fallback is owned by that work.
- Shared blocker record: `docs/change-requests/2026-10-21-agent-runtime-guardrails-and-evidence-blockers.md`.
