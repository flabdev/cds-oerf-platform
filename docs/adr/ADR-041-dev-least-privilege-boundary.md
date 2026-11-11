# ADR-041: Least-privilege dev deployments and a permissions boundary

- **Status:** Accepted
- **Date:** 2026-11-11
- **Tracker:** A2, E2, E8 (raised in the review of #48)

## Context

Dev and main share one account (ADR-037). The dev CloudFormation execution role started with `AdministratorAccess`, which made the stage separation a naming convention only: a dev template could change main. Within one account IAM can narrow the boundary but cannot make it absolute.

## Decision

1. **Least-privilege CloudFormation execution role for dev.** The `oerfdev` bootstrap's execution role carries two allow policies (`OerfDevCfnExecPolicy`, `OerfDevCfnExecPolicy2`) and a deny policy (`OerfDevCfnExecDenies`) instead of `AdministratorAccess`. All are managed in `OerfCiStack-dev`.
2. **Permissions boundary on every dev role** (`OerfDevWorkloadBoundary`), including the execution role and all workload roles; dev templates cannot create a role without it, and nothing in dev can remove it.
3. **How dev resources are recognised:**
   - **By name** where names are fixed: `-dev-`, `_dev_`, `/dev/`, `Stackdev`, `oerfdev` patterns, per service.
   - **By tag** (`oerf:stage=dev`) for services whose ARNs carry generated ids: Cognito user pools, API Gateway, CloudFront distributions. Tagging those services is limited so main's resources cannot be tagged dev.
   - **By ARN** where a service does not pass resource tags to IAM: the dev Bedrock guardrail (`dev:guardrailArn`).
4. **Explicit denies** in both the deny policy and the boundary: IAM users and access keys, user and group policies, removing a boundary, editing these policies, protected roles (GitHub deploy, bootstrap, main), administrator-type policy attachments, Organizations and account actions, KMS key administration.
5. **Checks:**
   - `scripts/check_dev_boundary.py` simulates allowed and denied calls against the deployed roles, without tag context (what real calls do) and with it (the policy logic).
   - Every CloudFormation handler permission for every resource type in the dev templates is checked against the policies, using AWS's published handler permissions and service reference (`infra/bootstrap/iam-fixtures/`). Permissions tied to properties the dev templates do not use are marked not applicable, never allowed blindly.
6. **Managed policy names and descriptions are pinned** by a test: changing either forces a replacement that fails with a fixed name.

## Consequences

- A dev template, or a dev workload role, cannot read or change main's resources in the checked services.
- Every new resource type in a dev template needs a fixture refresh (`scripts/refresh_iam_fixtures.sh`) and may need a policy change; the handler check fails until it is handled.
- The boundary is near the 6,144-character managed-policy limit; additions need compression first.
- Residual risks remain where IAM cannot scope: WebSocket `execute-api` posting, SMS publish, Step Functions task tokens, some account-level reads. They are listed in `docs/security/dev-cfn-exec-handler-permissions-report.md`.
- IAM's simulator does not model every action (four SNS subscription actions are reported as unverifiable); the first real deploy that exercises them is watched in CloudTrail.

## Alternatives considered

- **Keep `AdministratorAccess` for dev:** fastest, but no real separation; rejected after review.
- **Tags only:** Bedrock guardrails do not pass resource tags to IAM, so a tag-only fence denied dev's own guardrail; name and ARN rules are used where tags fail.
- **Separate dev account with an SCP:** the complete answer; tracked as E8.
