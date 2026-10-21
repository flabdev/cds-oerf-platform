# OERF-207 — Evidence ingestion, S3 storage and vision-assisted verification

**Issue type:** Story · **Sprint:** B · **Estimate:** 1 day


**Depends on:** OERF-206 model/vision decision, OERF-112 incident storage and Ananda's S3/IAM wiring.

### Description

**PR #5 overlap (merged):** PR #5 is on `main`, so `oerf/platform/evidence/vision.py` and `tests/unit/evidence/test_vision.py` exist. Review that input/output schema, confidence handling, fault-catalog validation, tenant/privacy boundaries and permissions against this ticket; reuse and complete that module rather than building a second vision handler. Section 4 of the shared blocker record lists what it is missing today.
Handle inbound image/media evidence securely, persist it under a tenant/incident-scoped S3 key, invoke vision analysis behind an injectable service boundary, validate extracted fault codes against the catalog, and apply the specification confidence gate. AI is advisory; unverified evidence cannot trigger unsafe actions.

### Inputs/outputs
Input metadata includes tenant, incident, engagement, provider/media ID, content type, checksum and upload reference. Output includes evidence ID, S3 key, checksum, media metadata, extracted observations, catalog match, confidence and review-required decision.

### Implementation steps
1. Validate tenant/incident engagement authorization and media size/type; reject unsupported content before download.
2. Retrieve media through an injected provider interface; never log raw media or credentials.
3. Compute checksum and store immutable object under `evidence/{tenant_id}/{incident_id}/{evidence_id}` with encryption and metadata; no cross-tenant object access.
4. Invoke vision model via injected client/provider; constrain output schema and retain model/version/provenance.
5. Validate extracted fault code against configured catalog; unknown/mismatched code goes to manual review.
6. Apply spec §24 confidence threshold and require explicit human confirmation for unsafe/low-confidence result.
7. Persist evidence metadata and link into incident/report workflow; retry safely by evidence ID/checksum.
8. Test fake object store/model, invalid media, duplicate upload, wrong tenant, unknown code and confidence below/above threshold.

### Acceptance criteria
- [ ] Evidence is tenant/incident scoped, immutable, checksummed and safely validated.
- [ ] Vision outputs are schema-checked and fault-code catalog validated.
- [ ] Confidence gate is enforced; model never makes an unsafe autonomous decision.
- [ ] Idempotent retries do not duplicate evidence; no PII/media in logs.
- [ ] Unit tests pass; required S3/IAM contract is documented for Ananda.

### Implementation boundary and blockers

- `TODO(OERF-310)`: `GUARDRAIL_ID` and `GUARDRAIL_VERSION` are unset, `oerf/platform/evidence/vision.py` calls `converse` with no `guardrailConfig`, and the vision role's `bedrock:InvokeModel` resource is the model ARN only. The guardrail half of this ticket waits on that.
- `TODO(OERF-301)`: no new wiring is needed for the upload half. `oerf-inbound` already has `EVIDENCE_BUCKET_NAME` and evidence-bucket `put`, and the vision function reads its object from the event's `bucket` and `key`.
- Findings already visible in `oerf/platform/evidence/vision.py` that this ticket owns: the whole event is logged (including the object key); extracted `fault_codes` are returned without catalog validation; there is no confidence gate; and the `evidence_analysis` write uses `pk=TENANT#{tid}` with `sk=INCIDENT#{iid}`, which does not match section 4 of the contract.
- Inbound media is not stored anywhere yet. `oerf/platform/inbound/handler.py` only caps and projects each media entry into the resumed payload, so this ticket owns writing the object and linking it to the incident.
- Shared blocker record: `docs/change-requests/2026-10-21-agent-runtime-guardrails-and-evidence-blockers.md`.
