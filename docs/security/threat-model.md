# OERF threat model

Tenant isolation is the security boundary. A caller, connection, reading, or
inbound message never names another tenant as input that the platform trusts.

## Actors

- **Ops user / tenant admin.** Cognito groups on `oerf-users`. Tenant is
  `custom:tenant_id` on the token, copied by the pre-token trigger.
- **Platform admin.** May choose a tenant only on `/admin/tenants/...`.
- **Driver / facility / QA.** Party roles do not log in. They reply on WhatsApp
  or SMS. Ownership is `PHONE#` plus `ADDR#`, not a body `tenant_id`.
- **Simulator.** EventBridge `sim.reefer` readings. Ingest trusts `DEVICE#`,
  not `detail.tenant_id` when they disagree.

## Assets

Incident META (`TENANT#{tid}#INC#{iid}`), engagement tokens on `ENG#`, inbound
media under `tenants/{tid}/inbound/`, report objects
`reports/{tid}/{iid}/v{n}.html`, Cognito users, and the dashboard WebSocket.

## Trust boundaries

| Surface | Tenant source | Rejects |
| --- | --- | --- |
| Dashboard REST | Access/ID token `custom:tenant_id` | Body `tenant_id` mismatch, missing claim |
| HTTP API GET | Same claim | Cross-tenant incident id (404) |
| WebSocket `$connect` | `GetUser` attribute | Missing tenant, unknown client id |
| Subscribe | Bound connection tenant | Body `tenant_id`, `ALL`, `*` |
| Ingest | `DEVICE#` registry | Unknown device, tenant/shipment mismatch |
| Inbound | Stored `OUTMSG#` / `ADDR#` / `PHONE#` | Spoofed `tenant_id` or `task_token` on the payload |
| Sandbox `JOIN` | Unregistered number only | A number already on a non-sandbox tenant |

## Controls

- Dashboard actions enqueue SQS; they do not call `SendTaskSuccess`.
- Resume tokens live on `ENG#` / `OUTMSG#`, never on legacy `MSG#`.
- Reports are HTML, no SES on the report role.
- Live WhatsApp IAM is the phone-number-id ARN when `channels:live` is set.
- IAM `*` exceptions are listed in [iam-exceptions.md](iam-exceptions.md).

## Suite

Run `pytest -q -m security`. CI runs that marker as its own step. Findings
and residual risk are in [findings.md](findings.md).
