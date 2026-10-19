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
and `allowance_remaining` for the claimed tenant.

`GET /incidents/{id}` adds `timeline`, `options`, `recommendation`,
`approval`, `evidence`, and `engagement_status`.

The query port is `IncidentQuery` in `oerf/platform/api/dashboard.py`.
OERF-208 supplies the production adapter. The route does not write incident
state.

## Actions

`POST /incidents/{id}/actions` accepts `confirm_transfer`, `pick_option`,
and `cancel`. `pick_option` requires `option_id`. The response is `202`
and `action_id`. The same `Idempotency-Key` returns the same `action_id`
and does not enqueue a second message.

SQS message body:

```json
{
  "action_id": "uuid",
  "action": "confirm_transfer",
  "option_id": null,
  "tenant_id": "t1",
  "incident_id": "INC-000001",
  "actor": "cognito-sub"
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
