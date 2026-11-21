# ADR-043: Jev as an optional backend for free-text driver replies

- **Status:** Proposed
- **Date:** 2026-11-21
- **Amends:** ADR-042 (which classifier proposes; nothing about who decides)

## Context

ADR-042 lets a model propose a closed-vocabulary classification of a free-text driver reply on `critical_field`, and Interpret validates it. The proposer today is a Strands agent on AgentCore calling Nova Lite. It writes JSON, and its `confidence` is a number the model writes about itself. The tenant floor (`evidence.min_confidence`, 0.8) is compared with that number, so the floor is only as good as the model's self-report.

TypeSafe's Jev is a decision model. It does not generate text: a `choice` question lists its options, and the answer is one of those option keys with a confidence and a probability per option. The vendor describes the probabilities as calibrated. That claim is not independently verified.

## Decision

1. **Same contract, second proposer.** `INTERPRET_CLASSIFIER=jev` replaces the AgentCore call with `JevReplyClassifier` (`oerf/platform/cadence/jev_classifier.py`). It returns the ADR-042 proposal shape, and `validate_proposal`, the tenant floor, the `critical_field` scope, and the routing are unchanged. AgentCore stays the default. The CDK flag `jev:enabled` selects Jev and fails synth if `agentcore:enabled` is also true.
2. **One closed question.** Jev gets one `choice` question with eight options. Each option maps onto one ADR-042 category and one proposed intent: `unit_running`, `unit_stopped`, and `unknown` map to `answer`; `call_me` to `call-me`; `decline`, `help`, `concern`, and `unrelated` to their own categories. Interpret then clarifies everything except `answer` and `call-me`. The reply cannot steer Jev outside those keys.
3. **Values are not the model's job.** Jev does not extract values. A fault code is found deterministically: a lenient pattern finds candidates (`E17`, `e-17`, `E 1 7`), and only a single code that the tenant `FAULTCODE#` catalog lists is proposed. `setpoint_temp_c` is not extracted on this path.
4. **Pinned and strict.** The model is pinned (`jev-1.14.0`, context `jev:model`), and a response served by another version is refused, so a decision is reproducible. Non-200 statuses, timeouts, malformed bodies, and options that were not offered all clarify. There are no retries. The Jev call is limited to 3 seconds, inside the 30-second Interpret budget.
5. **Minimal data out.** Only the reply text and the question go to TypeSafe. The tenant id, incident id, and phone number stay in AWS. The key is an SSM SecureString (`/oerf[/dev]/jev/api-key`) that an operator writes. Interpret reads only that parameter, and the key is never in the template. Traces carry the model id, latency, token counts, and an outcome token, never the reply.
6. **Shadow before switch.** `python -m scripts.eval_driver_replies_live --backend jev --confirm-jev-call` scores Jev on the corpus through the same gate as Bedrock and groups accuracy by confidence band. Enable `jev:enabled` in an environment only when Jev's category accuracy is at least Bedrock's and its `>=0.9` band is right about 90% of the time.

## Consequences

- Driver reply text leaves AWS for a third-party processor when the flag is on. This needs a data-processing review before it is used with real drivers. The corpus used for scoring is synthetic.
- The confidence floor becomes a comparison against a probability, if the calibration holds on our data.
- A Jev outage or a version retirement degrades to `clarify` and the existing ladder. It never blocks an engagement.
- Fault-code extraction is narrower than the generative path: two-character-prefix codes in other formats are not found, and setpoints are not extracted.

## Alternatives considered

- **Keep only AgentCore and Nova:** this is still the default. It keeps data in AWS but leaves the floor comparing against a self-reported number.
- **Cloudflare Clef (open weights):** Clef accepts the same request format. It could be self-hosted in AWS to keep the data inside, but it has no documented version pinning on Workers AI. Revisit it if data residency rules out TypeSafe.
- **Ask Jev for the fault code:** rejected. Jev picks options. It does not extract values, and a free-form code from a model would need the same catalog check anyway.
- **Extend Jev to facility and approval replies:** out of scope. ADR-042 and interface v1.58 limit free-text classification to `critical_field`.
