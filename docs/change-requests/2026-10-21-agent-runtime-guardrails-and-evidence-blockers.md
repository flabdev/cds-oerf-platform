# Change request: blockers for OERF-206 and OERF-207

**Priority:** normal — records what is missing; nothing here is urgent
**To:** Ananda (OERF-301, OERF-310, OERF-312) and the OERF-206 / OERF-207 owners
**From:** OERF-204 / OERF-205 (ingest and inbound handlers)
**Date:** 2026-10-21
**Contract:** [docs/interfaces.md](../interfaces.md) sections 4, 8 and 9; [ADR-020](../adr/ADR-020-bedrock-model.md); ADR-017

## 1. What is already wired

Verified against `infra/stacks/compute_stack.py` and `tests/unit/test_compute_stack.py`.

- Ingest: the `sim.reefer` / `TemperatureReading` rule, its dead-letter queue, `states:StartExecution` and the function environment are deployed. `oerf.platform.ingest.handler.handler` is now the real handler (OERF-204).
- Inbound: SQS trigger with `ReportBatchItemFailures`, `states:SendTaskSuccess`, and an environment that already carries `TENANT_TABLE_NAME`, `INCIDENT_TABLE_NAME`, `ROUTING_TABLE_NAME`, `PLATFORM_TABLE_NAME` and `EVIDENCE_BUCKET_NAME`, plus `s3:PutObject` on the evidence bucket (OERF-205).
- Vision: `amazon.nova-lite-v1:0` through the Converse API, `USE_MOCK_BEDROCK` defaulting to mock, and `bedrock:InvokeModel` scoped to the model ARN. `tests/unit/evidence/test_vision.py` asserts a single model call and no Claude model id, so ADR-020 is satisfied in code.
- The evidence analyzer takes `bucket` and `key` from its event, so it needs no `EVIDENCE_BUCKET_NAME` variable.

## 2. OERF-312 — `AGENT_RUNTIME_ARN` (blocks OERF-206)

The variable is reserved in section 9 and set nowhere, and there is no AgentCore runtime entry point in the tree. The Interpret state has nothing to invoke, so the agent half of OERF-206 cannot start.

Acceptance: the ARN is set on whichever function invokes the runtime, with permission to invoke only that runtime.

## 3. OERF-310 — guardrails and model access (blocks the guardrail half of OERF-206 and OERF-207)

`GUARDRAIL_ID` and `GUARDRAIL_VERSION` are reserved and unset. `oerf/platform/evidence/vision.py` calls `converse` with no `guardrailConfig`, while section 8 says the vision role calls Bedrock "with the guardrail id from the environment".

Acceptance: both values are set on the vision function, the call passes `guardrailConfig`, and a model change updates the `bedrock:InvokeModel` resource in the same change — ADR-020 notes the Haiku inference profile is not in-region on-demand.

## 4. OERF-207 — evidence findings to fix in `oerf/platform/evidence/vision.py`

1. It logs the whole event (`json.dumps(event)`) and the S3 key on failure. The ticket requires no PII or media in logs; log the incident and evidence id instead.
2. Extracted `fault_codes` are returned without catalog validation and there is no confidence gate, although the ticket requires both before anything downstream may act on the result.
3. It writes `evidence_analysis` with `pk=TENANT#{tid}` and `sk=INCIDENT#{iid}`, which does not match section 4 (`pk=TENANT#{tid}#INC#{iid}`, `sk=META`). The same shape appears in `oerf/platform/api/action_handler.py` and `oerf/platform/report/generator.py`, and `oerf/platform/channels/router.py` uses `pk=TENANT#{tid}#INCIDENT#{iid}`. The shared builder is `oerf.platform.storage.dynamo.incident_pk`. This needs one decision, then one fix per writer.
4. Inbound media is still not stored. `oerf-inbound` already has `EVIDENCE_BUCKET_NAME` and evidence-bucket `put`, but `oerf/platform/inbound/handler.py` writes no objects; the resumed payload only carries capped `id`/`mime_type` per media item.

## 5. OERF-301 — no DynamoDB TTL on any table

`INGEST#{event_id}` rows (OERF-204) and `INMSG#{provider_msg_id}` rows (OERF-205) carry `expires_at`, but no table in `infra/stacks/data_stack.py` sets `time_to_live_attribute`, so nothing expires and the rows accumulate.

Acceptance: TTL on the attribute the writers already use, or an explicit decision that retention is handled some other way, recorded in section 4 of the contract.

## 6. What this note does not ask for

- No handler ticket changes `infra/`. OERF-204 and OERF-205 keep their changes inside `oerf/platform/`.
- No second entry point for ingest or inbound. The deployed handler strings stay `oerf.platform.ingest.handler.handler` and `oerf.platform.inbound.handler.handler`.
- No change to ADR-020's model choice; the code already matches it.
