# Jev decision model (free-text driver replies)

Interpret can ask TypeSafe's Jev decision model first, before the AgentCore runtime (Nova), to propose a classification for a free-text driver reply on `critical_field`. AgentCore is asked only when Jev fails or is unsure. Jev only proposes. Interpret still decides with the same `validate_proposal`, tenant floor, and routing ([ADR-042](adr/ADR-042-driver-reply-classification.md), [ADR-043](adr/ADR-043-jev-reply-classifier.md)).

This page is the operator runbook. Jev is off by default, so AgentCore alone stays the default. See [agentcore.md](agentcore.md).

## Where Jev helps, and where it does not

| Question in the workflow | Jev? | Why |
| --- | --- | --- |
| Driver free text on `critical_field`: is the unit running, stopped, unknown, or is it a call-me, decline, help, concern, or unrelated reply? | **Yes** | This is a closed choice. Jev answers with one option key and a probability, so the reply cannot make it emit anything else, and the 0.8 floor compares against a probability instead of a number the model wrote about itself. |
| Fault code in that reply (`E17`, `e-17`, `E 1 7`) | No: a pattern plus the tenant `FAULTCODE#` catalog | Jev picks options. It does not extract values. Only a single catalog code is kept. |
| Setpoint temperature in that reply | No | Not extracted on the Jev path. The AgentCore path can propose it. |
| Facility and approver free text (`partner_request`, `approval_fast`) | No | ADR-042 and interface v1.58 limit free-text classification to `critical_field`. These stay on buttons and keywords. |
| Buttons and known words | No | `map_reply` handles them without any model call. |
| Divert approval, recovery choice, containment | No | These are rules (`assess`, `approval`, `verify`). A model never decides them. |

## Behaviour

1. A driver reply on `critical_field` that matches no button or keyword reaches Interpret.
2. Interpret reads the tenant policy (floor and fault-code catalog), as it does for AgentCore.
3. `JevReplyClassifier` sends one `choice` question to `POST https://api.typesafe.ai/v1/systemone`. The state is the question we asked the driver and the reply text. The tenant id, incident id, and phone number are not sent.
4. The chosen option maps to an ADR-042 proposal:

   | Jev option | Category | Proposed intent | Interpret outcome |
   | --- | --- | --- | --- |
   | `unit_running` / `unit_stopped` / `unknown` | `answer` | same | Answered, when confidence ≥ floor |
   | `call_me` | `call-me` | `call_me` | HandleCallback, when confidence ≥ floor |
   | `decline` | `decline` | `decline` | clarify (ResetLadder) |
   | `help` / `concern` / `unrelated` | same | `clarify` | clarify (ResetLadder) |

5. `validate_proposal` applies the floor, the field rules, and the `critical_field` intent set.
6. **Fallback.** When Jev errors (no key, timeout, non-200, malformed body, a different model version) or its confidence is below the tenant floor, Interpret asks AgentCore, if `agentcore:enabled` is on, with one attempt. A confident Jev answer stops there, even a `decline` or `help` that clarifies. If AgentCore also fails, or is off, the reply clarifies. The engagement never fails because of Jev.
7. The response carries `source`: `jev` or `llm` (AgentCore), whichever proposal decided it. Interpret sets it.
8. Each Jev call writes a sanitized trace: model id, latency, token counts, and an outcome token (`OK`, `TIMEOUT`, `HTTP_429`, `MODEL_MISMATCH`, …). The reply text and the key are never logged.

## What deploys

Context `jev:enabled` defaults to **false**. When it is true, Interpret gets:

| Variable | Value |
| --- | --- |
| `JEV_API_KEY_PARAM` | `/oerf/jev/api-key` on main, `/oerf/dev/jev/api-key` on dev |
| `JEV_MODEL` | context `jev:model`, default `jev-1.14.0` |
| `JEV_TIMEOUT_SECONDS` | `3` (Interpret accepts 1–5) |

The Interpret role gets `ssm:GetParameter` on that one parameter. The key is read with decryption on first use and cached for the life of the container.

`jev:enabled` works alone (Jev only, no fallback) or with `agentcore:enabled` (Jev, then AgentCore). The AgentCore settings are unchanged. Dev deploys already pass `-c agentcore:enabled=true`, so adding `-c jev:enabled=true` gives the cascade.

Time budget, inside the unchanged 30-second Interpret timeout:

| Step | Worst case |
| --- | --- |
| Tenant read | 3 s |
| Jev key read (first call per container) | 3 s |
| Jev call | 3 s (at most 5) |
| AgentCore, one attempt after Jev | 11 s |
| **Total** | **20 s (at most 22)** |

## Turning it on

1. **Data-processing review.** With Jev on, driver reply text goes to TypeSafe, outside AWS. Get that signed off before real drivers use it. The scoring corpus is synthetic.
2. **Store the key** (an operator does this; it is never in the template):

   ```bash
   aws ssm put-parameter --name /oerf/dev/jev/api-key --type SecureString --value '<key>'
   ```

3. **Score it beside Bedrock.** All runs use the same corpus and the same validation gate:

   ```bash
   python -m scripts.eval_driver_replies_live --confirm-bedrock-call --json > nova.json
   JEV_API_KEY='<key>' python -m scripts.eval_driver_replies_live --backend jev --confirm-jev-call --json > jev.json
   JEV_API_KEY='<key>' python -m scripts.eval_driver_replies_live --backend cascade \
       --confirm-jev-call --confirm-bedrock-call --json > cascade.json
   ```

   Compare `accuracy` (category), `by_asl_route` (route), `by_confidence`, and, for the cascade, `by_source`. Enable only when the cascade's category accuracy is at least Bedrock's and Jev's `>=0.9` band is right about 90% of the time.
4. **Deploy** with `-c jev:enabled=true`, keeping `-c agentcore:enabled=true` for the fallback.
5. **Check** an Interpret response for a free-text reply shows `source: jev`, and its trace shows `outcome: OK` and `model_id: jev-1.14.0`.

## Turning it off

Deploy without `jev:enabled` (or with it false). Interpret goes back to AgentCore alone if `agentcore:enabled` is true, or to buttons and keywords only. No data migrates. The SSM parameter can stay or be deleted.

## Upgrading the model

The model is pinned. A response from another version is refused (`MODEL_MISMATCH`), so the reply falls back to AgentCore, or clarifies without it. To move to a new version, re-run the scorer with `JEV_MODEL` set, then deploy with `-c jev:model=<version>`. If TypeSafe retires the pinned version, every reply goes to AgentCore (or clarifies) until the pin moves.

## Code

- `oerf/platform/agent/jev.py`: the HTTP client. Standard library only, HTTPS only, no retries, strict parsing.
- `oerf/platform/cadence/jev_classifier.py`: the question, the option mapping, and the fault-code check.
- `oerf/platform/cadence/interpret.py`: the proposer order, the fallback rule (`is_unsure`), `source`, and the key read.
- `infra/stacks/compute_stack.py`: the `jev:enabled` flag, environment, and IAM.
- `scripts/eval_driver_replies_live.py`: `--backend jev` and `--backend cascade` scoring.
