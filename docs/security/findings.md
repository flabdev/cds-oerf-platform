# Security findings (SWEEP-1)

Status is in-repo unless noted. Live confirmation waits on the first **dev**
deploy (sheet A6).

## Demo blockers (E1)

| Id | Finding | Status |
| --- | --- | --- |
| SEC-01 | WebSocket `$connect` must accept the dashboard hosted-UI client, not only the HTTP API client. | In code: SSM `/oerf/ws-allowed-clients/dashboard`; desk `VITE_USER_POOL_CLIENT_ID` is that client. Verify on a live socket after A6. |
| SEC-03 | Non-admin cannot publish config; T2 allowance publish is audited. | Unit tests in `tests/unit/api/test_dashboard.py`. |
| SEC-12 | CloudFront CSP. | Custom `DashboardSecurityHeaders` policy on `OerfApiStack` (default-src none, connect-src Cognito and execute-api). |

## Rest of the sweep (E2)

| Id | Finding | Status |
| --- | --- | --- |
| SEC-02 | Least-privilege IAM; remaining `*` in [iam-exceptions.md](iam-exceptions.md). | Synth assertions in compute/data/api tests. |
| SEC-04 | No default tenant on unauthenticated calls. | `tests/unit/security/test_no_default_tenant.py`. |
| SEC-06 | Redact phones, tokens, and secrets from handler logs and WS envelopes. | Broadcaster and desk event tests. |
| SEC-07 | TLS on buckets and APIs. | `enforce_ssl` and HTTPS-only CloudFront. |
| SEC-08 | Evidence expiry. | Evidence bucket lifecycle 90 days. |
| SEC-10 | Synth must not embed secret-shaped values. | `tests/unit/security/test_synth_secrets.py`. |
| Pagination | `GET /incidents` pages by incident id cursor, max 50. | `oerf/platform/api/queries.py`. |
| Secrets scan | Working-tree gitleaks. | `.gitleaks.toml`; CI `gitleaks detect --no-git` runs before pytest. `tests/unit/security/test_gitleaks_config.py` records the config checksum. History, on a full clone: `gitleaks detect --source . --config .gitleaks.toml`. |
| Login enumeration | Cognito told a caller whether the username existed. | Dashboard user-pool client sets `PreventUserExistenceErrors=ENABLED`. |

## Older tickets (E6)

Checked in this repo. A live dev execution is still required before calling them accepted.

| Ticket | What the repo shows |
| --- | --- |
| OERF-208 | Action idempotency is covered by the action-handler and inbound tests. The trigger route stays retired. |
| OERF-112a | Incident GSI1 is on the data stack and `GET /incidents` queries it. Verify the index on the deployed table. |
| OERF-118 | The scripted responder is the demo reply path (`scripts.demo_reply`). It refuses `--live`. |

## Contract sign-off (G3)

`docs/interfaces.md` is v1.43. Vivek signed v1.40 for OERF-206 on 2026-11-04. OERF-201 through OERF-205 and OERF-207 still need their owners. This change does not record those signatures.

## Residual

- Demo numbers live in SSM, never in this repo or the tracker sheet.
- Unseeded Cognito users have no `custom:tenant_id` until `scripts.seed_demo_users`.
- Sandbox JOIN is in-process; channel send of `judge_welcome` still uses empty catalog entries until templates are filled.
