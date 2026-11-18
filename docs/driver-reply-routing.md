# Driver reply corpus: validated intent and engagement route

This table documents the OERF-212 route for all 42 privacy-safe corpus rows in
`tests/ai-evals/driver_replies.jsonl`. It describes **deterministic offline
routing fixtures**, not model performance. The test fixture supplies an
approved synthetic catalog containing E17, E05, E22, and E09 so code-answer
rows can exercise the valid route. These fixture entries are not tenant data
and do not imply that any real tenant has approved those codes.

`answer` rows use `unit_running` as a deterministic valid example of the
permitted `unit_running` / `unit_stopped` / `unknown` answer intents; the corpus
labels do not themselves encode unit status. `decline` uses `decline`, and
`call-me` uses `call_me`. `help`, `concern`, and `unrelated` are rejected as
answer intents and clarify.

| Row | Corpus category (`expected_intent`) | Fixture validated / clarify intent | ASL route | Outcome |
| --- | --- | --- | --- | --- |
| DR-001 | clean_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-002 | clean_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-003 | clean_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-004 | clean_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-005 | clean_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-006 | misspelt_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-007 | misspelt_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-008 | misspelt_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-009 | misspelt_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-010 | misspelt_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-011 | misspelt_code (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-012 | free_text (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-013 | free_text (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-014 | free_text (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-015 | free_text (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-016 | free_text (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-017 | free_text (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-018 | hinglish (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-019 | hinglish (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-020 | hinglish (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-021 | hinglish (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-022 | hinglish (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-023 | hinglish (`answer`) | unit_running | AcknowledgeReply → Answered | consumed |
| DR-024 | concern (`concern`) | clarify | ResetLadder | not consumed |
| DR-025 | concern (`concern`) | clarify | ResetLadder | not consumed |
| DR-026 | concern (`concern`) | clarify | ResetLadder | not consumed |
| DR-027 | concern (`concern`) | clarify | ResetLadder | not consumed |
| DR-028 | help (`help`) | clarify | ResetLadder | not consumed |
| DR-029 | help (`help`) | clarify | ResetLadder | not consumed |
| DR-030 | help (`help`) | clarify | ResetLadder | not consumed |
| DR-031 | call_me (`call-me`) | call_me | HandleCallback | callback requested; no answer acknowledgment |
| DR-032 | call_me (`call-me`) | call_me | HandleCallback | callback requested; no answer acknowledgment |
| DR-033 | call_me (`call-me`) | call_me | HandleCallback | callback requested; no answer acknowledgment |
| DR-034 | unrelated (`unrelated`) | clarify | ResetLadder | not consumed |
| DR-035 | unrelated (`unrelated`) | clarify | ResetLadder | not consumed |
| DR-036 | unrelated (`unrelated`) | clarify | ResetLadder | not consumed |
| DR-037 | decline (`decline`) | decline | AcknowledgeReply → Answered | consumed |
| DR-038 | decline (`decline`) | decline | AcknowledgeReply → Answered | consumed |
| DR-039 | driving_safety (`help`) | clarify | ResetLadder | not consumed |
| DR-040 | driving_safety (`help`) | clarify | ResetLadder | not consumed |
| DR-041 | driving_safety (`help`) | clarify | ResetLadder | not consumed |
| DR-042 | driving_safety (`help`) | clarify | ResetLadder | not consumed |

## Fail-closed catalog condition

At present there is no authoritative per-tenant catalog provisioned for these
codes. Accordingly, any answer proposal containing a fault code (DR-001–DR-011)
currently validates as `clarify` and routes through `ResetLadder`, not
`AcknowledgeReply` / `Answered`. `tests/unit/evals/test_driver_reply_routing.py`
checks both the positive synthetic-catalog paths above and these missing-catalog
failure paths. Never treat the synthetic fixture as a production allow-list.

Classifier errors, malformed or low-confidence proposals, and any unmatched
intent follow the same safe clarification route. ASL routing receives only the
validated `$.response.intent` / `$.response.valid` contract; no classifier or
task token is passed into the classifier from ASL.
