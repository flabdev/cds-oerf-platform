# ADR-002: Pooled multi-tenancy

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Lead

## Decision

One shared deployment serves every tenant. `tenant_id` is part of every DynamoDB key, S3 prefix, log line, and Step Functions execution name. A domain is a playbook template: schema, defaults, state machine, and message catalog. A tenant subscribes to a domain and overrides settings. Product profiles override inside a tenant.

Tenant identity on a request comes from the Cognito claim `custom:tenant_id` ([ADR-034](ADR-034-cognito-tenant-claim.md)), from the device registry, or from a stored incident or engagement. It does not come from a request body or query string.

## Reasons

A stack or account per tenant is more infrastructure than this release needs. Separate tables for shared compute add operational cost without a stronger boundary than key design and the authorizer already give.

## Consequences

The repository layer and the authorizers enforce the boundary. An early note also named IAM `LeadingKeys` conditions. Deployed policies do not use that condition.

Per-tenant quotas are deferred. The sandbox join cap ([ADR-032](README.md)) is a sandbox-only control.
