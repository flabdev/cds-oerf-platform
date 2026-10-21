# Dashboard interfaces

These contracts are what the dashboard API, the action queue, and the existing
WebSocket stream handler share. `infra/app.py` is not changed here; the
application-stack owner registers `OerfApiStack`.

## Identity

Cognito groups are `platform_admin`, `tenant_admin`, and `ops_user`.
The tenant claim is `custom:tenant_id`. Incident routes use that claim.
A different `tenant_id` in the body is rejected. A `tenant_admin` may call
admin routes only for the claimed tenant. A `platform_admin` may name the
tenant in `/admin/tenants/{tid}`.

## Incident reads

`GET /incidents` returns `incident_id`, `state`, `stage`, `shipment_id`,
and `allowance_remaining` for the claimed tenant. `limit` defaults to 50
and `next_cursor` is the last id on a full page.

`GET /incidents/{id}` adds `timeline`, `options`, `recommendation`,
`approval`, `evidence`, and `engagement_status`.

The query port is `IncidentQuery` in `oerf/platform/api/dashboard.py`.
OERF-208 supplies the production adapter. The route does not write incident
state.

## Actions

`POST /incidents/{id}/actions` accepts `confirm_transfer`, `pick_option`,
and `cancel`. `pick_option` requires `option_id`. `confirm_transfer` is
allowed in `ASSESSING`, `AWAITING_APPROVAL`, `SECURING_FACILITY`, and
`IN_TRANSIT`. `pick_option` is allowed in `ASSESSING` and
`AWAITING_APPROVAL`. `cancel` is allowed until `CLOSED` or `CANCELLED`.
`ops_user`, `tenant_admin`, and `platform_admin` may enqueue. The response
is `202` and `action_id`. The same `Idempotency-Key` returns the same
`action_id` and does not enqueue a second message. A queue failure is `503`
and does not change the incident.

SQS message body:

```json
{
  "action_id": "uuid",
  "action": "confirm_transfer",
  "option_id": null,
  "tenant_id": "t1",
  "incident_id": "INC-000001",
  "actor": "cognito-sub",
  "channel": "dashboard",
  "type": "text",
  "text": "confirm_transfer"
}
```

The message has no task token. The action Lambda may send to the FIFO queue
and may not call Step Functions or write the incident table.

## Admin

- `GET` and `PUT /admin/tenants/{tid}/domains/{domain}/config` read the
  published document and save a draft.
- `POST .../config/validate` and `POST .../config/publish` use the OERF-113
  publisher. Publish writes the next immutable version and does not change
  an incident's pinned snapshot.
- `GET` and `PUT /admin/tenants/{tid}/contacts|facilities|faultcodes`.
- `GET` and `PUT /admin/tenants/{tid}/domains/{domain}/cadence` validates
  the cadence schema.
- `GET` and `POST /admin/tenants` are limited to `platform_admin`.

Each write appends an audit event with `actor`, `role`, `tenant_id`,
`operation`, and `at`.

## WebSocket

The dashboard socket uses the existing connection handler. `$connect`
requires the Cognito token query parameter. `subscribe` stores the
connection against the tenant bound at connect, not a tenant in the payload.

Fanout payload already emitted by the stream broadcaster:

```json
{
  "type": "INCIDENT_UPDATE",
  "eventName": "MODIFY",
  "tenant_id": "t1",
  "incident_id": "INC-000001",
  "status": "ASSESSING",
  "timeline": [],
  "evidence_analysis": null,
  "updated_at": 0
}
```

A connection receives that event only when its subscription tenant matches
and its incident is `ALL` or the same incident id.

## Sandbox inbound

OERF-205 owns normalization, dedupe, and task-token resume. After
`normalize_inbound`, the sandbox ingress calls `handle_sandbox_message` in
`oerf/platform/sandbox/join.py`. Mudita owns that handler. It does not call
`apply_opt_out`, because that would change every tenant registered on the
phone.

The whole trimmed message `JOIN` (any case) from a number with no production
phone-index row creates an `sbx` contact, a shipment session, and opt-in.
Consent time and the inbound channel are stored on the session. The session
expires 24 hours after it starts. The same number, while that session is
active, gets the same introduction again and does not take another daily
slot. A second inbound id for a message that was already answered returns
the stored result.

The phone index is read only to see whether any row belongs to a tenant
other than `sbx`. Those rows are not copied into the reply, and JOIN does
not write them. A production number is rejected. `STOP` or `UNSUBSCRIBE`
changes opt-in only for the `sbx` contact and blocks a later provider send.

Platform rows use partition key `PLATFORM#sandbox` on the routing table:
`SETTINGS` (`join_phone` or `dashboard_only`) and `JOIN#{YYYY-MM-DD}` (UTC,
maximum 20). `reset_sandbox_tenant` deletes `sbx` session, shipment, and
contact rows. It does not delete or reset those platform rows. Deployment
reset (Ananda, OERF-305) must use that function rather than wiping
`PLATFORM#sandbox`.

`dashboard_only` creates no contact and does not call a provider. The reply
has `simulated` true, `delivered` false, and channel `dashboard`.
`route_outbound` returns the same shape, with reason `simulated`, when it
is given the sandbox store and the tenant is `sbx`.

## Environment

| Variable | Used by |
| --- | --- |
| `INCIDENT_TABLE_NAME` | dashboard query Lambda |
| `TENANT_TABLE_NAME` | dashboard query Lambda |
| `ACTION_QUEUE_URL` | dashboard action Lambda |
| `ROUTING_TABLE_NAME` | WebSocket connect and subscribe |
| `USER_POOL_ID` | WebSocket authorizer |
| `USER_POOL_CLIENT_ID` | WebSocket authorizer |
| `REPORT_PDF_BACKEND` | `weasyprint` or `none`; HTML is always produced |
