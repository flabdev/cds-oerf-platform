# Change request: Cognito `custom:tenant_id`

**Priority:** HIGH
**To:** data-stack owner
**From:** OERF-304
**Date:** 2026-10-18
**Contract:** [docs/interfaces.md](../interfaces.md) section 2
**File you own:** `infra/stacks/data_stack.py` (`OerfUserPool`, pool name `oerf-users`)

## Why this blocks the demo

The frozen contract takes tenant only from the claim `custom:tenant_id`. The pool does not define that attribute.

- The WebSocket authorizer calls `GetUser` and denies `$connect` when the attribute is empty ([oerf/platform/api/ws_authorizer.py](../../oerf/platform/api/ws_authorizer.py)).
- The HTTP action handler reads `custom:tenant_id` from the JWT ([oerf/platform/api/action_handler.py](../../oerf/platform/api/action_handler.py)). With no claim, the tenant is null and the route cannot scope the call.

Groups `platform_admin`, `tenant_admin`, and `ops_user` already exist. This request does not change them. `custom:role` is not part of the contract; do not add it.

## Do not replace the user pool

`OerfUserPool` has `RemovalPolicy.RETAIN`. That does not make a schema edit safe.

Cognito custom attributes are part of the pool schema. In CloudFormation, `AWS::Cognito::UserPool` `Schema` is create-time. A CDK change that sets `custom_attributes` on the existing `UserPool` usually shows **Replacement: True** in `cdk diff`. Replacement creates a new pool and leaves the old one retained and unused. The app client in `OerfComputeStack` (`oerf-http-api-client`) would point at the new empty pool. Users on the old pool would no longer authenticate.

Before you merge:

1. Run `cdk diff` and read the `OerfUserPool` action.
2. If the plan says the pool will be replaced, do not deploy that change.
3. Add the attribute with Cognito `AddCustomAttributes` (a custom resource or a one-off API call) so the physical pool id stays the same. Confirm the diff shows no replacement of `AWS::Cognito::UserPool` and no replacement of the app client.

Suggested attribute: name `tenant_id` (Cognito stores it as `custom:tenant_id`), string, mutable so an admin can correct a user, not required at sign-up (self sign-up is already off).

## The attribute alone does not fix HTTP

`GetUser` returns user attributes, so the WebSocket path works once each demo user has `custom:tenant_id` set.

The HTTP API JWT authorizer sees the access token. Cognito access tokens do not include custom attributes unless a pre-token-generation trigger (V2) copies `custom:tenant_id` onto the access token. Please either:

- add that trigger on this pool, without replacing the pool, or
- agree with OERF-301/302 that the trigger lives in `OerfComputeStack` and you only add the attribute.

Existing users need the attribute set (`AdminUpdateUserAttributes`). A new schema attribute does not backfill them. Demo seeding (OERF-305) will set it for new users after the attribute exists.

## Acceptance

- `cdk diff` shows no replacement of `OerfUserPool`.
- A user in group `ops_user` with `custom:tenant_id=t1` produces that claim on the token the HTTP authorizer validates, and `GetUser` returns the same attribute.
- No passwords, tokens, or account ids are committed.
