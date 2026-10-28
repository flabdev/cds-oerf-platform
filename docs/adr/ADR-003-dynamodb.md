# ADR-003: DynamoDB as the data store

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

Amazon DynamoDB, on-demand, is the system of record. There are four tables: `platform`, `tenant`, `incident`, and `routing`. The incident table has streams enabled (`NEW_AND_OLD_IMAGES`).

## Reasons

The data is structured and the access patterns are known. A relational database would put the functions in a VPC so they could reach the rest of the platform. DynamoDB keeps the deployment serverless and gives the desk a stream ([ADR-010](ADR-010-live-updates.md)).

## Consequences

There are no foreign keys and no ad-hoc queries. Integrity checks live in JSON Schema and the repository layer. Access patterns are fixed in [the interface contract](../interfaces.md). The repository layer is the place a later store would plug in.

Incident list-by-status uses GSI1 ([ADR-035](ADR-035-incident-gsi1.md)). One open incident per shipment uses a lock item ([ADR-016](README.md)), which replaced a second index.
