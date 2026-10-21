# OERF-204 — Temperature ingest Lambda: normalize, debounce, trigger, lock and start execution

**Issue type:** Story · **Sprint:** A · **Estimate:** 1 day


**Depends on:** OERF-107/OERF-108 existing domain APIs and OERF-112 persistence/debounce.
**Blocks:** end-to-end incident start and OERF-305.

### Description
Implement EventBridge ingest for temperature readings. Validate/normalize event, dedupe event/action identity, update debounce counter, evaluate trigger using configured tenant shipment/profile, conditionally open incident and acquire shipment lock, then start state-machine execution. Preserve valid PutEvents event format: tenant/shipment live inside `detail`.

### Inputs/outputs
Input EventBridge event with `source=sim.reefer`, `detail-type=TemperatureReading`, and detail fields `tenant_id`, `shipment_id`, `device_id`, `ts`, `temperature_c`, `lat`, `lon`, `seq`. Output is structured accepted/ignored/duplicate/triggered result with incident/execution identifiers; EventBridge invocation can use standard Lambda return semantics.

### Suggested modules
`oerf/platform/ingest/handler.py` plus unit tests. Inject config, trigger evaluator, debounce, dedupe, incident repository, open lock and Step Functions client.

### Implementation steps
1. Validate event shape and reject malformed/missing required tenant/shipment/device/time/sequence/temperature values without trusting arbitrary body tenant fields outside detail.
2. Apply idempotency using stable event ID/action ID; duplicate returns without side effects.
3. Resolve tenant config and shipment; update debounce on out-of-range readings and clear on safe readings.
4. Call `evaluate_trigger` only once configured consecutive breach threshold is met; do not duplicate the threshold logic in the handler.
5. Create incident and acquire open lock with race-safe conditional semantics; handle existing open incident idempotently.
6. Start execution with stable execution name and frozen envelope, pinning config version/snapshot and relevant reading context.
7. Ensure handler has narrow error classification, structured logging identifiers, and no ambient time dependency.
8. Test malformed/valid/duplicate/safe/debounce threshold/concurrent trigger/already-open incident/StartExecution failure cases.

### Acceptance criteria
- [ ] Accepts EventBridge-shaped detail; ignores/rejects invalid events safely.
- [ ] Debounce + trigger behavior is deterministic and threshold-safe.
- [ ] Incident/lock and execution start are idempotent under duplicate or concurrent readings.
- [ ] Config snapshot is pinned to incident; tenant isolation tested.
- [ ] Unit and integration tests pass; no infrastructure changes in this ticket.

### Implementation boundary and external wiring

- `oerf.platform.ingest.handler` is the deployed `oerf-ingest` entry point. The compute stack already wires the `sim.reefer` / `TemperatureReading` EventBridge rule with a dead-letter queue, two retries, a one-hour maximum event age, `states:StartExecution` on the cold-chain state machine, and the environment `INCIDENT_TABLE_NAME`, `TENANT_TABLE_NAME`, `ROUTING_TABLE_NAME`, `EVENT_BUS_NAME`, `STATE_MACHINE_ARN`, `LOG_LEVEL`. This ticket changes no infrastructure.
- Event identity, debounce streaks and the shipment lock are kept on the incident table, because `oerf-ingest` has read-only access to the routing and tenant tables and no platform-table variable. `EVENT_BUS_NAME` is for publishers and is not read by this handler.
- `TODO(OERF-301)`: no DynamoDB TTL is configured on any table, so the `expires_at` attribute on the `INGEST#{event_id}` dedupe rows and the debounce streaks does not delete them yet. Retention is written but inert until the data stack enables TTL on the incident table.
- `TODO(OERF-116)`: the repository provides an injected `DEVICE#{device_id}` resolver contract but no device-registry writer or seeded rows. Ingest fails closed for unknown devices; device registration and the routing-table write permission remain owned by that work.
- Log lines carry identifiers only (tenant, shipment, incident). Reading values from the untrusted EventBridge `detail` — temperature, coordinates, sequence — are never logged or echoed in the Lambda result.
