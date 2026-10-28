# ADR-009: Thin admin portal

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

Tenant settings are edited in the same desk application, with draft, validate, and publish. A published version is immutable. An open incident keeps the snapshot it was given ([ADR-013](ADR-013-config-snapshot.md)).

## Reasons

Multi-tenancy has to be visible without a second application. Generating forms from the domain schema would let a new domain avoid new UI code.

## Consequences

The deployed desk does not do that. There is no schema-generated admin route. A tenant admin can publish a product-profile allowance. That publish writes a new config version. Incidents already open keep the allowance they started with.

The JSON Schema form generator described in the early note is not in the desk.
