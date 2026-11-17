# ADR-042: Classify free-text driver replies with rule-owned routing

- **Status:** Accepted
- **Date:** 2026-11-17
- **Tracker:** OERF-209

## Context

The existing inbound handler correlates a driver reply to one engagement and resumes that engagement's task token. Interpret already maps known buttons and keywords without calling AgentCore. Free-text replies need a closed-vocabulary interpretation to continue the existing ASL engagement safely; the model must not become a workflow authority.

## Decision

1. **Keep the boundary:** AgentCore may propose a constrained classification for bounded free text only. Deterministic Interpret validation owns final `response.intent` and `response.valid`. The model cannot send messages, write incident state, resume a task token, or select an arbitrary workflow action.
2. **Keep labels distinct:** `response.category` is the corpus `expected_intent`/FR-M3 label (`answer`, `decline`, `help`, `concern`, `call-me`, `unrelated`). The dataset's descriptive evaluation-slice `category` (for example `clean_code`, `hinglish`, or `driving_safety`) remains a separate evaluation-only value. `response.intent` is the ASL-routing vocabulary: `unit_running`, `unit_stopped`, `unknown`, `decline`, `call_me`, or `clarify`. The unsupported corpus label `answer` is never sent to ASL as an intent.
3. **Validate the proposal:** the proposal has only `category`, proposed `intent`, `fields`, and `confidence`. `fields` is allow-listed to `fault_code` and numeric `setpoint_temp_c`; confidence is in [0, 1]. Text-only Phase 1 has no evidence references. Proposed values are not authoritative. Fault-code extraction is unverified until it passes the existing tenant catalog policy; missing or unreadable catalog data fails closed.
4. **Use the approved routing:** `answer` maps to `unit_running`, `unit_stopped`, or `unknown`; only a schema- and policy-valid answer is valid. A fault-code field must pass tenant catalog validation. A valid answer follows existing `AcknowledgeType?` → `AcknowledgeReply` → `Answered`. `decline` maps to valid `decline` and the existing valid-answer acknowledgment/`Answered` path. `call-me` maps to valid callback intent `call_me` and existing `HandleCallback`. `help`, `concern`, `unrelated`, and any unmatched, malformed, low-confidence, unavailable, or timed-out classification map to invalid `clarify` and existing `ResetLadder`; these do not consume the reply.
5. **Preserve ingress authority:** inbound remains the only component that resumes a task token. Known buttons and keywords bypass AgentCore. The reply text is sensitive, bounded to 4,000 characters (the existing inbound limit), and is not newly persisted or written to logs or traces.
6. **Keep Phase 1 narrow:** no media interpretation, facility/operations classification, generated chat, or new ASL state.

## Consequences

- Existing task-token lifecycle, correlation, and state-machine structure remain unchanged. Unclear replies use `clarify` and the existing reset/retry behavior.
- The classifier may be unavailable or return unusable output without advancing an engagement on an unsupported answer; Interpret fails closed to clarification.
- E17/E05/E22/E09 are not asserted to exist in a tenant catalog. They cannot be accepted as verified unless an authoritative tenant source supplies the corresponding `FAULTCODE#` rows.
- OERF-210–214 implement and validate the contract; the interface and runtime changes remain subject to their owners' sign-off.

## Alternatives considered

- **Let the model choose an arbitrary ASL route or mark its own answer valid:** rejected; the workflow boundary must remain deterministic.
- **Pass corpus `answer` directly to ASL:** rejected; it is an evaluation label, not a supported ASL route.
- **Treat extracted fault codes as trusted or add a global allow-list:** rejected; policy must be tenant-scoped and fail closed.
- **Add new states for unclear replies:** rejected; existing `clarify`/`ResetLadder` behavior is sufficient.
