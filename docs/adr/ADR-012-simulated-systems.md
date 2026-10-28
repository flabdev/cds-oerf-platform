# ADR-012: Simulate external systems

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

A deterministic Python simulator emits reefer telemetry. Seed data supplies tenants, shipments, facilities, and contacts. Distances and drive times are precomputed in that seed. There is no live IoT, transport, or warehouse integration.

## Reasons

The playbook has to be repeatable for a demo and for tests. A live sensor feed is not required to prove orchestration, messaging, or the desk.

## Consequences

The simulator publishes `TemperatureReading` to the EventBridge bus `oerf-events` with source `sim.reefer`. Ingest trusts the device registry, not a tenant id in the event body. An unknown device is dropped. A body tenant that disagrees with the registry is dropped.

Map links in a divert notice are static coordinates. They are not a routing API.
