# ADR-034: Cognito tenant claim, without replacing the pool

**Status:** Accepted
**Date:** 2026-10-18
**Decider:** Architect

## Context

Every authorized call takes its tenant from the claim `custom:tenant_id`. The WebSocket authorizer reads it from `GetUser`. The HTTP authorizer reads it from the access token. Cognito does not put custom attributes on an access token unless a pre-token-generation trigger copies them.

The user pool schema is create-only in CloudFormation. Setting `custom_attributes` on the existing pool replaces it. Replacement would leave the app client pointed at an empty pool.

## Decision

Keep the pool. A custom resource calls `AddCustomAttributes` for a string attribute `tenant_id` (stored as `custom:tenant_id`), mutable, not required, length 1 to 128. If the attribute is already on the pool, the resource does nothing. Delete does not remove it.

`oerf-pre-token` is a V2 pre-token-generation trigger. It copies `custom:tenant_id` onto the ID token and the access token, and writes the user's groups through `groupOverrideDetails`. The pool feature plan is Essentials, which that trigger version requires. Self sign-up stays off.

The claim is still the only tenant input on a request ([ADR-002](ADR-002-pooled-multi-tenancy.md)).

## Consequences

Existing users need the attribute set. A new schema attribute does not backfill them. Demo seeding sets it for users it creates.

The contract text that said the pool did not define the attribute is older than this stack. The data stack adds the attribute and the trigger.
