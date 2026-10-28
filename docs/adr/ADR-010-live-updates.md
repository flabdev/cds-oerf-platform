# ADR-010: Live updates

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

The incident table stream (`NEW_AND_OLD_IMAGES`) feeds `oerf-stream-broadcaster`. The function pushes to API Gateway WebSocket connections subscribed by tenant, and by incident when the subscription is incident-scoped.

`$connect` is authorized. The authorizer binds the connection to `custom:tenant_id`. A client cannot name a tenant in the message body.

## Reasons

The desk should move when the incident moves. Polling feels late. A GraphQL subscription layer is a second API for the same fan-out.

## Consequences

The stream filter is incident `META`. Reading rows and timeline rows are not events. A subscription with no scope receives nothing. A connection for one tenant does not receive another tenant's incidents.

The WebSocket API is `oerf-ws-api` in `OerfComputeStack`. The desk, when `OerfApiStack` is deployed, uses that endpoint.
