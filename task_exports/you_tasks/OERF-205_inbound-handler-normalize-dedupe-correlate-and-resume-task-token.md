# OERF-205 — Inbound handler: normalize, dedupe, correlate and resume task token

**Issue type:** Story · **Sprint:** B · **Estimate:** 1.5 days


**Depends on:** OERF-109/OERF-110 normalization APIs, OERF-115 engagement records, OERF-201 workflow.
**Blocks:** human-response E2E.

### Description
Consume normalized provider/dashboard inbound messages, apply opt-out precedence, deduplicate, resolve sender/correlation, validate reply against the waiting engagement, then resume the matching Step Functions task token. Unknown or late responses must be logged and safely ignored; tenant comes from trusted registry/correlation, never message body.

### Inputs/outputs
Input may be SQS record envelope for provider webhook/dashboard action. Normalize to canonical `{provider_message_id, sender, text, received_at, channel, media?, action_id?}`. Successful valid response issues `SendTaskSuccess` with sanitized payload; invalid/decline/error paths use agreed `SendTaskFailure`/workflow decision; duplicate/unknown/late results are acknowledged and logged.

### Implementation steps
1. Implement Lambda batch/SQS parsing and per-record error handling; use partial batch response where configured.
2. Run `normalize_inbound` and evaluate opt-out before message correlation.
3. Apply `InboundDedupe`; repeated provider ID/action ID causes no duplicate continuation.
4. Resolve by registered outbound message correlation or trusted sender registry as specified; never accept tenant ID asserted in message content.
5. Check engagement is WAITING and task token is still valid; late replies return harmlessly without calling Step Functions.
6. Verify/canonicalize response according to role/ask contract; cap payload and remove untrusted fields before `SendTaskSuccess`.
7. Send exactly once and handle token-expired/invalid-token service errors safely.
8. Test opt-out precedence, valid reply, duplicate, unknown sender, shared number, cross-tenant isolation, invalid payload, late reply and AWS error handling.

### Acceptance criteria
- [ ] Normalize→opt-out→dedupe→correlate→verify→resume ordering enforced.
- [ ] Tenant derived from trusted lookup, never body/query.
- [ ] Duplicate and late replies never resume workflow twice.
- [ ] Token success/failure payload matches engagement contract.
- [ ] Unit/integration tests pass; inbound handler alone needs `SendTaskSuccess` permission.

### Implementation boundary and external wiring

- `oerf.platform.inbound.handler` implements steps 1, 2, the `OUTMSG#` half of step 4, and the resume in step 5. Step 3 (`JOIN` and the sandbox counter), the `ADDR#` fallback with `disambiguation` and the out-of-context reply, and evidence storage of inbound media are not in that module; `docs/interfaces.md` section 5 records them as open.
- Reply classification stays in the engagement Interpret step (ADR-017), so this role sends no `states:SendTaskFailure`. A decline, cancellation, or unusable reply is resolved by the workflow decision branch; the rung keeps waiting instead.
- The resume payload is the task result at the state root, not a Lambda request/response envelope. The engagement step that replaces the `Approval` placeholder owns that reply contract.
- `TODO(OERF-301)`: deploy this entry point with an SQS trigger using `ReportBatchItemFailures`, and move `states:SendTaskSuccess` to the inbound role with its removal from the action handler (ADR-028). The deployed `oerf-inbound` environment already carries `INCIDENT_TABLE_NAME`, `ROUTING_TABLE_NAME`, `TENANT_TABLE_NAME`, `PLATFORM_TABLE_NAME` and `EVIDENCE_BUCKET_NAME`; no infrastructure is changed by this ticket.
- `TODO(OERF-116)`: the `ADDR#` correlation fallback is not implemented, so a reply that quotes no outbound message id is acknowledged and logged without a callback.
- `TODO(OERF-207)`: evidence storage for inbound media is owned by the evidence ticket. The inbound role already has `EVIDENCE_BUCKET_NAME` and evidence-bucket `put`, but this module writes no objects yet, so media is capped and projected to `id`/`mime_type` in the callback instead of being stored.
- `TODO(OERF-301)`: no DynamoDB TTL is configured on any table, so the `INMSG#` dedupe rows this module writes on the routing table are retained but never expire until TTL is enabled.
