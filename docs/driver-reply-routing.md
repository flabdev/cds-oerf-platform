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


## Reproducible evaluation

Run the credential-free report from the repository root:

```bash
python -m scripts.eval_driver_replies
python -m scripts.eval_driver_replies --json
```

The report retains the historical exact `map_reply` baseline as **0/42** and
separately reports the deterministic offline fixture evaluation. The fixture
mirrors the corpus labels/fields; its perfect result validates evaluator and
routing wiring only, **not a model or Bedrock score**. Per-row output includes
predicted corpus label, proposed fields, validated ASL intent, validity,
expected/actual route, and expected/actual consumption outcome. Breakdowns are
available by descriptive corpus category, ASL route, and final outcome.

The offline evaluator exercises the actual `interpret_workflow` validation
boundary. Its accepted corpus proposals use only a synthetic fixture catalog
(E17, E05, E22, E09), never an authoritative tenant allow-list. Use the test
suite to exercise rejected, malformed, low-confidence, timeout, exception, and
missing-catalog paths. Those paths clarify and do not consume the response.

## Fail-closed catalog condition

At present there is no authoritative per-tenant catalog provisioned for these
codes. Accordingly, in production any answer proposal containing a fault code
(DR-001–DR-011) validates as `clarify` and routes through `ResetLadder`, not
`AcknowledgeReply` / `Answered`. The offline report's 25 consumed rows assume the
synthetic fixture catalog above; with no catalog those code rows instead clarify,
and `scripts.eval_driver_replies` run with a missing catalog marks them
fail-closed. `tests/unit/evals/test_driver_reply_routing.py` checks both the
positive synthetic-catalog paths above and these missing-catalog failure paths.
Never treat the synthetic fixture as a production allow-list.

Classifier errors, malformed or low-confidence proposals, and any unmatched
intent follow the same safe clarification route. ASL routing receives only the
validated `$.response.intent` / `$.response.valid` contract; no classifier or
task token is passed into the classifier from ASL.
