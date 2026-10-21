# OERF interface contract

**FROZEN v1.2** — authored 2026-10-18 by OERF-304 (Ananda).

Change log:

- v1.2 (2026-10-21, WhatsApp) channel mocks default to true unless context `channels:live` is true. `WHATSAPP_PHONE_NUMBER_ID` comes from context `whatsapp:phoneNumberId`. `WA_PHONE_NUMBER_ID_PARAM` is set on the router.
- v1.1 (2026-10-21, OERF-301) adds env `INBOUND_QUEUE_URL` on the action handler.

OERF-306 maintains this file after the sign-off table is complete. Tickets may build against this text now. A signed row is that owner's acceptance of the sections they own.

The deployed runtime is `OerfComputeStack` in [infra/stacks/compute_stack.py](../infra/stacks/compute_stack.py). There is no `OerfAppStack` and no `api_stack.py`. Where a ticket uses those names, it means this stack.

## 1. Versioning and change control

Owner: OERF-304 until sign-off, then OERF-306.

Semver on this document:

- **Major.** A breaking change: removing or renaming a field, key, route, env var, or entry point, or tightening a required set in a way existing producers fail.
- **Minor.** An optional field or a new enum value that old consumers can ignore.
- **Patch.** Wording that does not change a parser, a key, or an IAM boundary.

Change process: a pull request, plus sign-off from the owner of each section the diff touches. OERF-306 merges documentation-only changes after freeze. A code owner does not edit another owner's handler to match a change; the PR updates this file, and the handler owner adopts it.

| Ticket | Owner | Signs | Status |
| --- | --- | --- | --- |
| OERF-201 | | | pending — name to be filled by Ananda |
| OERF-202 | | | pending — name to be filled by Ananda |
| OERF-203 | | | pending — name to be filled by Ananda |
| OERF-204 | | | pending — name to be filled by Ananda |
| OERF-205 | | | pending — name to be filled by Ananda |
| OERF-206 | | agent behaviour | pending — name to be filled by Ananda |
| OERF-207 | | vision behaviour | pending — name to be filled by Ananda |
| OERF-208 | Mudita | action handler, incident key, action route | pending |
| OERF-117 | Mudita | report generator | pending |
| OERF-301 | Ananda | env names (acked), `SendTaskSuccess` move, synth | pending |
| OERF-302 | Mudita | HTTP routes, WebSocket subscribe, list API | pending |
| OERF-308 | Ananda | stream broadcaster scopes and envelope | pending |

## 2. Tenant and auth claims

Owner: Mudita (OERF-302) for enforcement on HTTP and WebSocket. Data-stack owner for the user-pool attribute (change request below).

The user pool `oerf-users` has three groups: `platform_admin`, `tenant_admin`, `ops_user`.

| Claim | Source | Rule |
| --- | --- | --- |
| Tenant | `custom:tenant_id` | The only tenant input. The API copies it onto every write and every queued message. |
| Role | `cognito:groups` | Resolved by [oerf/platform/api/claims.py](../oerf/platform/api/claims.py). `platform_admin` wins when that group is present. An empty group list is `ops_user`. |
| Subject | `sub` | Stored as `user_id` on dashboard actions. |

`custom:role` is not part of this contract.

`platform_admin` may pass an explicit tenant only where an admin route says so (`/admin/tenants/...`). `tenant_admin` and `ops_user` operate inside the tenant on their token. Party roles (driver, facility, QA, and the escalation roles) do not log in.

**HIGH.** The pool does not define `custom:tenant_id`. [oerf/platform/api/ws_authorizer.py](../oerf/platform/api/ws_authorizer.py) denies `$connect` when the attribute is missing, and [oerf/platform/api/action_handler.py](../oerf/platform/api/action_handler.py) reads the same claim from the JWT. Until the attribute exists and is set on demo users, every authorized route has no tenant. Change request: [2026-10-18-cognito-custom-tenant-id.md](change-requests/2026-10-18-cognito-custom-tenant-id.md). The data-stack owner must confirm that the CDK change does not replace `OerfUserPool` (replacement would drop the pool the app client uses and strand existing users).

The HTTP API authorizer reads JWT claims. A Cognito access token does not carry custom attributes unless a pre-token-generation trigger copies them. The WebSocket authorizer calls `GetUser`, which returns user attributes. Both paths need the attribute on the user. The HTTP path also needs the claim on the access token. That second step is in the same change request.

Example access-token claims this contract expects:

```json
{
  "sub": "user-1",
  "cognito:groups": ["ops_user"],
  "custom:tenant_id": "t1",
  "token_use": "access",
  "client_id": "app-client-id"
}
```

## 3. Config and dashboard actions

### 3.1 Domain config

Owner: OERF-201 (name pending). The file on disk is [domains/cold_chain_excursion/schema.json](../domains/cold_chain_excursion/schema.json) (JSON Schema 2020-12).

Publish result, as produced by [oerf/platform/tenancy/publisher.py](../oerf/platform/tenancy/publisher.py):

```json
{
  "tenant_id": "t1",
  "domain": "cold_chain_excursion",
  "config_version": 2,
  "status": "published",
  "published_at": "2026-10-18T12:00:00Z",
  "published_by": "user-1",
  "previous_version": 1
}
```

ADR-013: incident open stores `config_version` and `config_snapshot` on the incident. Later publishes apply only to new incidents. Tenant keys: `PK=TENANT#{tid}`, `SK=CONFIG#{domain}#v{n}` and `SK=CONFIG#{domain}#ACTIVE`.

### 3.2 Dashboard actions

Owner: Mudita (OERF-208 implements the handler, OERF-302 owns the route).

Frozen route: `POST /incidents/{id}/actions`. Cognito JWT. The body schema rejects unknown fields, so `tenant_id` is not a client field.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://oerf.local/interfaces/dashboard-action.json",
  "type": "object",
  "additionalProperties": false,
  "required": ["action", "action_id"],
  "properties": {
    "action": { "enum": ["confirm_transfer", "pick_option", "cancel"] },
    "action_id": { "type": "string", "minLength": 1 },
    "engagement_id": { "type": "string", "minLength": 1 },
    "option_id": { "type": "string", "minLength": 1 },
    "reason": { "type": "string" }
  },
  "allOf": [
    {
      "if": { "properties": { "action": { "const": "confirm_transfer" } } },
      "then": { "required": ["engagement_id"] }
    },
    {
      "if": { "properties": { "action": { "const": "pick_option" } } },
      "then": { "required": ["engagement_id", "option_id"] }
    }
  ]
}
```

The API responds `202` with `{ "action_id": "<client id>", "status": "queued" }`. It writes an SQS message and returns. It does not call `states:SendTaskSuccess`.

Queued message (tenant and user come from the token):

```json
{
  "tenant_id": "t1",
  "incident_id": "INC-000123",
  "engagement_id": "ENG-03",
  "channel": "dashboard",
  "provider_msg_id": null,
  "context_msg_id": null,
  "action_id": "act-9f3c",
  "action": "pick_option",
  "option_id": "OPT-B",
  "user_id": "user-1",
  "type": "text",
  "text": null,
  "media_ref": null,
  "received_at": "2026-10-18T12:00:00Z"
}
```

Dedupe key: `INMSG#{action_id}` (section 5). An action whose engagement is no longer waiting is ignored and logged, without a task-token call.

Deployed deviation, Mudita (208/302) to migrate: `POST /incidents/{id}/action` accepts `APPROVE`, `REJECT`, and `OVERRIDE`, and the handler calls `SendTaskSuccess` itself.

`cancel` stops the execution, stops engagements, and deletes the shipment lock (ADR-019). It still enters through this queue so one component resumes or stops workflows.

## 4. Event envelopes

### 4.1 TemperatureReading

Owner: OERF-118 for the simulator payload. Ingest owner (name pending) for the accept/drop rule. OERF-301 wires the EventBridge rule.

The simulator keeps `tenant_id` and `shipment_id` on `Detail` (OERF-118). Bus name: `oerf-events`.

```json
{
  "Source": "sim.reefer",
  "DetailType": "TemperatureReading",
  "EventBusName": "oerf-events",
  "Time": "2026-10-20T10:16:00Z",
  "Detail": {
    "tenant_id": "t1",
    "shipment_id": "PH-2841",
    "device_id": "RF-7781",
    "ts": "2026-10-20T10:16:00Z",
    "temperature_c": 8.1,
    "lat": 17.44,
    "lon": 78.38,
    "seq": 412
  }
}
```

Ingest loads `PK=DEVICE#{device_id}`, `SK=META` on the routing table. That item's tenant and shipment are the ones used to open or update the incident. The event is dropped when `Detail.tenant_id` is present and differs from the registry tenant. An unknown device is dropped. The EventBridge rule matches source and detail-type only. Thresholds stay in tenant config.

### 4.2 Lifecycle

Owner: Ananda (OERF-308) as the stream consumer. No EventBridge consumer is registered. OERF-301 does not add a lifecycle rule until a consumer is named here.

```json
{
  "source": "oerf.incident",
  "detail-type": "IncidentStateChanged",
  "detail": {
    "tenant_id": "t1",
    "incident_id": "INC-000123",
    "from_state": "OPEN",
    "to_state": "DIAGNOSING",
    "version": 2,
    "emitted_at": "2026-10-18T12:00:00Z"
  }
}
```

`from_state` and `to_state` are states in [oerf/platform/incidents/repository.py](../oerf/platform/incidents/repository.py) (`OPEN`, `DIAGNOSING`, `ASSESSING`, `AWAITING_APPROVAL`, `SECURING_FACILITY`, `ESCALATED`, `IN_TRANSIT`, `VERIFYING`, `CONTAINED`, `REOPENED`, `AWAITING_DISPOSITION`, `CLOSED`, `CANCELLED`). The stream path is the only consumer. A future EventBridge put uses this body unchanged.

### 4.3 SQS inbound envelope

Owner: inbound handler (name pending, OERF-201–207). The queue message, after the normalizer and after correlation for provider traffic, and immediately for dashboard actions:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://oerf.local/interfaces/inbound-message.json",
  "type": "object",
  "additionalProperties": false,
  "required": ["channel", "received_at"],
  "properties": {
    "tenant_id": { "type": ["string", "null"] },
    "incident_id": { "type": ["string", "null"] },
    "engagement_id": { "type": ["string", "null"] },
    "channel": { "enum": ["whatsapp", "sms", "rcs", "email", "dashboard"] },
    "provider_msg_id": { "type": ["string", "null"] },
    "context_msg_id": { "type": ["string", "null"] },
    "action_id": { "type": ["string", "null"] },
    "action": { "enum": ["confirm_transfer", "pick_option", "cancel", null] },
    "option_id": { "type": ["string", "null"] },
    "user_id": { "type": ["string", "null"] },
    "type": { "enum": ["button", "text", "image", "audio", "document"] },
    "text": { "type": ["string", "null"] },
    "media_ref": { "type": ["string", "null"] },
    "received_at": { "type": "string", "minLength": 1 }
  }
}
```

Provider messages may omit tenant and incident until correlation fills them. Dashboard messages include both before they are queued. `tenant_id` on a dashboard message is the token claim.

Pre-correlation WhatsApp normalization is `NormalizedMessage` in [oerf/platform/inbound/normalize.py](../oerf/platform/inbound/normalize.py): `provider_msg_id`, `channel`, `sender`, `received_at`, `text`, `type` (`text|image|audio|document`), `media[]`. That model covers WhatsApp only. SMS, RCS, email, and dashboard are still this queue schema.

## 5. Inbound, JOIN, and correlation

Owner: inbound handler (name pending, OERF-201–207). Library pieces that already exist: `oerf.platform.inbound.normalize`, `dedupe`, `dynamo`. No inbound Lambda is deployed.

Order for a provider message:

1. Dedupe. Duplicate `INMSG#` rows are dropped.
2. Opt-out keywords (`STOP`, `UNSUBSCRIBE`, and the tenant's local equivalents) before correlation. Set opt-in false and stop that contact's engagements.
3. `JOIN` (exact text, case-insensitive) when `PHONE#{e164}` has no item whose sort key is a non-sandbox tenant. A number registered to any non-sandbox tenant is not joined. A repeat `JOIN` returns that judge's existing sandbox shipment. The daily counter is `PK=SANDBOX#JOINS#{date}`, `SK=META`, conditional increment while `count` is below `sandbox.max_joins_per_day` (platform setting, default 20). Over the cap, the reply logical name is `sandbox_full`. Under the cap, the sandbox tenant gains a contact (opted in) and a shipment, and the logical reply is `judge_welcome`. Both replies go out through the channel router.
4. Correlation. `context_msg_id` loads `OUTMSG#{id}`. With no context id, `ADDR#{channel}#{address}` loads open engagements: one match is used, several tenants get `disambiguation`, none gets the out-of-context reply.
5. Media is stored as evidence. The handler then calls `states:SendTaskSuccess` on the current rung token. Classification happens in the engagement Interpret step (ADR-017), not here.

Dashboard messages skip steps 2–4 and correlate on `engagement_id`.

Dedupe item, agreed by the spec and [oerf/platform/inbound/dedupe.py](../oerf/platform/inbound/dedupe.py):

| Key | Value |
| --- | --- |
| Table | routing |
| PK | `INMSG#{provider_msg_id}` or `INMSG#{action_id}` |
| SK | `META` |
| Retention | 7 days from accept time, not from `received_at` |

Correlation keys, agreed by the LLD and [oerf/platform/storage/dynamo.py](../oerf/platform/storage/dynamo.py):

| Key | Item |
| --- | --- |
| `OUTMSG#{provider_msg_id}` | Outbound id → tenant, incident, engagement. SK `ENG#{tid}#{iid}#{eid}`. |
| `ADDR#{channel}#{address}` | Address → open engagements. Same engagement SK. |
| `PHONE#{e164}` | SK `TENANT#{tid}#CONTACT#{cid}`. JOIN guard and correlation fallback (ADR-029). |

## 6. Repository access patterns

Owner: OERF-112 for the repository (Mudita). Data-stack owner for indexes. Authoritative incident key, from the LLD and `incident_pk()`:

`PK=TENANT#{tid}#INC#{iid}`, `SK=META`. Timeline rows: `SK=EVT#{iso-timestamp}#{seq}`.

Deployed deviation, Mudita (208): the action handler writes `PK=TENANT#{tid}`, `SK=INCIDENT#{id}`. That shape is not the contract.

Lock item (ADR-016): `PK=TENANT#{tid}#SHIP#{sid}`, `SK=OPEN_INCIDENT`, conditional put, deleted on close or cancel.

| # | Pattern | Table | Key |
| --- | --- | --- | --- |
| 1 | Tenant profile | tenant | `PK=TENANT#{tid}`, `SK=META` |
| 2 | Active domain config | tenant | `SK=CONFIG#{d}#ACTIVE`, then `SK=CONFIG#{d}#v{n}` |
| 3 | Config versions | tenant | `SK` begins_with `CONFIG#{d}#v` |
| 4 | Cadence, comms profile, calendar | tenant | `SK=CADENCE#{p}#v{n}`, `COMMS#{p}#v{n}`, `CALENDAR#{id}` |
| 5 | Contacts, facilities, fault codes | tenant | `SK` begins_with `CONTACT#`, `FACILITY#`, `FAULTCODE#` |
| 6 | Shipment | tenant | `SK=SHIPMENT#{id}` |
| 7 | Config audit | tenant | `SK` begins_with `AUDIT#` |
| 8 | Device → tenant, asset, shipment | routing | `PK=DEVICE#{id}`, `SK=META` |
| 9 | Open incident for a shipment | incident | lock item, pattern above |
| 10 | Full incident | incident | `PK=TENANT#{tid}#INC#{iid}` |
| 11 | Timeline in order | incident | `SK` begins_with `EVT#` |
| 12 | Incidents by status | incident GSI1 | `GSI1PK=TENANT#{tid}#STATUS#{state}`, `GSI1SK=opened_at` |
| 13 | Reply by outbound id | routing | `PK=OUTMSG#{id}` |
| 14 | Open engagements for an address | routing | `PK=ADDR#{channel}#{address}` |
| 15 | Inbound dedupe | routing | conditional put `PK=INMSG#{id}`, `SK=META` |
| 16 | Domain template | platform | `PK=DOMAIN#{d}`, `SK=TEMPLATE#v{n}` |
| 17 | Phone registry | routing | `PK=PHONE#{e164}` |
| 18 | Sandbox JOIN counter | routing | `PK=SANDBOX#JOINS#{date}`, `SK=META` |
| 19 | Platform settings | platform | `PK=SETTINGS`, `SK=PLATFORM` |

Pattern 12 is not deployed. The incident table has no GSI, and the data-stack unit test asserts that. `GET /incidents` (OERF-302) depends on it. Change request: [2026-10-18-incident-gsi1-by-status.md](change-requests/2026-10-18-incident-gsi1-by-status.md).

S3 prefixes start with `tenants/{tid}/`.

## 7. API and WebSocket events

Owner: Mudita (OERF-302) for routes and the subscribe message. Ananda (OERF-308) for the broadcast payload. Both sign this section.

### 7.1 HTTP API

API `oerf-http-api`. Cognito JWT authorizer, identity source `Authorization`. CORS allows `Content-Type` and `Authorization`.

| Route | Contract |
| --- | --- |
| `POST /incidents/{id}/actions` | Section 3.2. Frozen. |
| `GET /incidents/{id}` | Incident META for the token tenant and path id: `incident_id`, `tenant_id`, `shipment_id`, `state`, `reason`, `opened_at`, `config_version`. `config_snapshot` is included on this authenticated read. `pk` and `sk` stay off the wire. |
| `GET /incidents` | List for the token tenant. Blocked on GSI1 (pattern 12). Each row is the summary object in section 7.3. |

Deployed and non-conformant: `POST /incidents/{id}/action`, and `POST /incidents/trigger` (the action handler writes an incident and calls `StartExecution`). Ingest is the EventBridge path in section 4.1. Whether `POST /incidents/trigger` remains as a demo hook is open (section 14).

### 7.2 Subscribe

WebSocket API `oerf-ws-api`, stage `prod`. `$connect` is authorized. The client sends the Cognito access token as the `token` query parameter. The authorizer binds the connection to `custom:tenant_id`.

Route `subscribe`. Two scopes, both the bound tenant:

```json
{ "scope": "incident", "incident_id": "INC-000123" }
```

```json
{ "scope": "tenant" }
```

`incident` requires `incident_id`. `tenant` rejects `incident_id`. A body `tenant_id` is not a field; a client that sends one fails validation. The values `ALL` and `*` are rejected. The deployed handler treats a missing incident id as `ALL` and accepts `*`. That behaviour is non-conformant. Mudita (302) and Ananda (308) migrate it.

Connection items stay on the routing table: `PK=WS#{connection_id}` / `SK=METADATA`, and `PK=WS_SUB#{tenant_id}` / `SK=CONN#{connection_id}`, with `scope` and, for incident scope, `incident_id`.

### 7.3 Dashboard events

Shared by OERF-302 and OERF-308. Times are UTC `YYYY-MM-DDTHH:MM:SSZ`.

`version` is the integer on incident META when that attribute exists. Until OERF-112 stores one, the broadcaster sets `version` to the count of `EVT#` rows, or `1` when there are none. `stage` is the playbook stage when the workflow has set it. Until then, `stage` equals `state`.

Incident scope (`event_type` `INCIDENT_UPDATE`). Sanitized projection. Allowed `data` keys are fixed; adding a key is a minor version of this contract.

```json
{
  "event_type": "INCIDENT_UPDATE",
  "tenant_id": "t1",
  "incident_id": "INC-000123",
  "version": 4,
  "emitted_at": "2026-10-18T12:00:00Z",
  "scope": "incident",
  "data": {
    "incident_id": "INC-000123",
    "state": "VERIFYING",
    "stage": "verify",
    "version": 4,
    "shipment_id": "PH-2841",
    "updated_at": "2026-10-18T12:00:00Z",
    "config_version": 2,
    "from_state": "IN_TRANSIT",
    "to_state": "VERIFYING",
    "latest_reading": { "ts": "2026-10-18T11:59:30Z", "temperature_c": 7.4 },
    "timeline": [{ "type": "state_changed", "at": "2026-10-18T12:00:00Z", "state": "VERIFYING" }]
  }
}
```

`from_state` and `to_state` are present only on a state transition, and they match section 4.2. Timeline entries are `{type, at, state}` only.

Tenant-list scope (`event_type` `INCIDENT_SUMMARY`). `data` is exactly these four fields:

```json
{
  "event_type": "INCIDENT_SUMMARY",
  "tenant_id": "t1",
  "incident_id": "INC-000123",
  "version": 4,
  "emitted_at": "2026-10-18T12:00:00Z",
  "scope": "tenant",
  "data": {
    "incident_id": "INC-000123",
    "state": "VERIFYING",
    "stage": "verify",
    "version": 4
  }
}
```

Neither payload carries a raw DynamoDB image, message text, `evidence_analysis`, phone numbers, names, task tokens, prompts, images, or model output. A record with no tenant id is skipped. A connection subscribed to tenant A never receives tenant B. An incident subscription receives only that incident's `INCIDENT_UPDATE`.

Deployed deviation: [oerf/platform/api/stream_broadcaster.py](../oerf/platform/api/stream_broadcaster.py) sends `type: INCIDENT_UPDATE` plus `timeline` and `evidence_analysis`, and it delivers every incident in the tenant to `ALL` and `*` subscriptions.

## 8. Handler entry points

Owner of the table: OERF-304. Each row's owner owns the module. All of these functions, once deployed, live in `OerfComputeStack`. One role per function. Do not add a second function for a row that already exists.

IAM column is the target. Current wildcards are called out and are not the target.

| Module | Function | Owner | Trigger | IAM target | Status |
| --- | --- | --- | --- | --- | --- |
| `domains.cold_chain_excursion.steps.context` | `handler` | pending (OERF-201–207) | Step Functions | Read tenant and incident for the execution's tenant. No messaging, no task token. | not deployed |
| `domains.cold_chain_excursion.steps.assess` | `handler` | pending (OERF-201–207) | Step Functions | Read incident and config snapshot. Write `OPTION#` items. No messaging. | not deployed |
| `domains.cold_chain_excursion.steps.act` | `handler` | pending (OERF-201–207) | Step Functions | Read/write incident state for act transitions. Sends go through the router, not from this role. | not deployed |
| `domains.cold_chain_excursion.steps.verify` | `handler` | pending (OERF-201–207) | Step Functions | Read incident and readings. Write containment state. | not deployed |
| `domains.cold_chain_excursion.steps.qa` | `handler` | pending (OERF-201–207) | Step Functions | Read incident and report pointer. No SES. | not deployed |
| `domains.cold_chain_excursion.steps.close` | `handler` | pending (OERF-201–207) | Step Functions | Write `CLOSED`, delete the lock item. No messaging. | not deployed |
| `oerf.platform.ingest.handler` | `handler` | pending (OERF-201–207) | EventBridge `sim.reefer` / `TemperatureReading` | Read routing device item and tenant config. Transact incident META plus lock. `states:StartExecution` on `STATE_MACHINE_ARN` only. | not deployed |
| `oerf.platform.inbound.handler` | `handler` | pending (OERF-201–207) | SQS inbound queue | Read/write routing and incident. Write evidence objects. `states:SendTaskSuccess` and `states:SendTaskFailure`. No channel-send APIs. | not deployed |
| `oerf.platform.channels.router` | `handler` | Mudita | Step Functions / direct invoke | Read/write routing (`OUTMSG#`, `ADDR#`). The only role with WhatsApp, SMS, and SES send. | deployed `oerf-channel-router` |
| `oerf.platform.report.generator` | `handler` | Mudita (OERF-117) | Step Functions | Read incident. Write `REPORT_BUCKET_NAME`. SES send is not the target (ADR-008). | deployed `oerf-report-generator` |
| `oerf.platform.api.stream_broadcaster` | `handler` | Ananda (OERF-308) | Incident table stream | Read incident stream. Read routing subscriptions. Delete gone connections. `execute-api:ManageConnections` on this API. No Step Functions, no incident writes. | deployed `oerf-stream-broadcaster` |
| `oerf.platform.api.ws_handler` | `handler` | Mudita (OERF-302) | `$connect`, `$disconnect`, `subscribe` | Read/write routing connection items only. | deployed `oerf-ws-handler` |
| `oerf.platform.api.ws_authorizer` | `handler` | Mudita (OERF-302) | WebSocket `$connect` authorizer | `cognito-idp:GetUser`. Write the connection binding. | deployed `oerf-ws-authorizer` |
| `oerf.platform.api.action_handler` | `handler` | Mudita (OERF-208) | HTTP API | Read token claims. Send the action message to the inbound queue. Read incident for `GET`. No `states:SendTaskSuccess`. | deployed `oerf-action-handler` |
| `oerf.platform.evidence.vision` | `handler` | OERF-207 (behaviour, name pending). Ananda (OERF-310) for model and guardrail env. | Step Functions / async invoke | Read evidence object. Write the evidence result on the incident. `bedrock:InvokeModel` on the approved model ARN only, with the guardrail id from the environment. | deployed `oerf-evidence-analyzer` |
| AgentCore runtime | entry owned by OERF-206 | OERF-206 behaviour. Ananda (OERF-312) for `AGENT_RUNTIME_ARN`. | Invoke from Interpret | Read tools' data through the tool layer. No incident transition, no channel send. | not deployed |

Current IAM that the target replaces:

- Channel router: `ses:SendEmail`, `sns:Publish`, `socialmessaging:*` on `*`.
- Evidence analyzer: `bedrock:InvokeModel` on `*`. No guardrail.
- Report generator: `ses:SendEmail` on `*`.
- Action handler: `states:SendTaskSuccess` and `states:SendTaskFailure` on `*`. Removal of `SendTaskSuccess` from this role is Ananda (OERF-301). The inbound role is the one that receives it (ADR-028). `SendTaskFailure` follows the same split.

`states:SendTaskSuccess` and `states:SendTaskFailure` are not resource-scopable. The wildcard resource stays, limited to the inbound role, and is recorded in `docs/security/iam-exceptions.md` when that file exists.

Package markers for `domains.cold_chain_excursion.steps` are not in the tree yet. OERF-301 wires the handler strings above and does not invent a second path.

## 9. Runtime environment

Owner: Ananda (OERF-301). Env-name convention acked by 301.

Resource identifiers use the deployed suffix. OERF-301's short names (`TENANT_TABLE`, `INCIDENT_TABLE`, `ROUTING_TABLE`, `PLATFORM_TABLE`, `EVIDENCE_BUCKET`, `REPORT_BUCKET`) are not used.

This ticket does not rename variables on deployed functions. OERF-301 sets missing names on new functions and adds a missing name to an existing function only when that function starts reading it.

| Name | Status | Set by |
| --- | --- | --- |
| `TENANT_TABLE_NAME` | required on new readers | OERF-301 |
| `INCIDENT_TABLE_NAME` | deployed | compute stack |
| `ROUTING_TABLE_NAME` | deployed | compute stack |
| `PLATFORM_TABLE_NAME` | required on new readers | OERF-301 |
| `EVIDENCE_BUCKET_NAME` | required when a function reads or writes evidence. Vision does not read this variable yet. | OERF-301 |
| `REPORT_BUCKET_NAME` | deployed on the report function | compute stack |
| `EVENT_BUS_NAME` | value `oerf-events`. Required on ingest and any publisher. | OERF-301 |
| `STATE_MACHINE_ARN` | deployed on the action handler. Ingest reads it for `StartExecution`. | compute stack |
| `INBOUND_QUEUE_URL` | Inbound queue URL. Set on the action handler. OERF-208 reads it to send the dashboard action. | OERF-301 |
| `CONFIG_ROOT` | directory of domain packages inside the asset. Value `domains`. The config library currently resolves the installed `domains` package and does not read this variable. | OERF-301, when a function needs an explicit root |
| `LOG_LEVEL` | logging level name. Default `INFO` when unset. | OERF-301 / OERF-303 |
| `USER_POOL_ID` | deployed on the WebSocket authorizer | compute stack |
| `USER_POOL_CLIENT_ID` | deployed on the WebSocket authorizer | compute stack |
| `WEBSOCKET_ENDPOINT` | deployed on the broadcaster | compute stack |
| `WHATSAPP_PHONE_NUMBER_ID` | Router. Value is context `whatsapp:phoneNumberId`, default empty. | WhatsApp infra |
| `SES_SOURCE_EMAIL` | deployed on the router and the report function | compute stack |
| `USE_MOCK_WHATSAPP` | Router. Default `true`. `false` only when context `channels:live` is true. | WhatsApp infra |
| `USE_MOCK_SMS` | Router. Same default as `USE_MOCK_WHATSAPP`. | WhatsApp infra |
| `USE_MOCK_EMAIL` | Router. Same default as `USE_MOCK_WHATSAPP`. | WhatsApp infra |
| `USE_MOCK_BEDROCK` | deployed on vision, default `false` | OERF-310 |
| `BEDROCK_MODEL_ID` | deployed on vision. Reserved for the value OERF-310 approves. | OERF-310 |
| `WA_PHONE_NUMBER_ID_PARAM` | Router. Value `/oerf/whatsapp/phone-number-id`. The handler does not read it yet. | WhatsApp infra |
| `GUARDRAIL_ID` | reserved. Not set. | OERF-310 |
| `GUARDRAIL_VERSION` | reserved. Not set. | OERF-310 |
| `AGENT_RUNTIME_ARN` | reserved. Not set. | OERF-312 |

Provider secrets stay in Secrets Manager, referenced by name or ARN. They are not environment variables.

Synth: [infra/app.py](../infra/app.py) takes the account from `CDK_DEFAULT_ACCOUNT`, then context `account`, then the placeholder `000000000000`. A CloudFormation rule rejects a deploy of that placeholder. Region defaults to `us-east-1`.

## 10. Logger

Owner: OERF-303. Module to add: `oerf/platform/observability/logging.py`. This section is the placeholder. OERF-303 fills the function signature and publishes it here. Handler owners adopt it in their own PRs.

One JSON object per line, UTC timestamp, correlation ids when the caller has them:

```json
{
  "timestamp": "2026-10-18T12:00:00Z",
  "level": "INFO",
  "message": "incident opened",
  "tenant_id": "t1",
  "incident_id": "INC-000123",
  "engagement_id": "ENG-03"
}
```

Absent ids are omitted. The logger must not accept task tokens, message bodies, phone numbers, names, prompts, images, raw model output, or raw DynamoDB images. OERF-303 defines what happens when a caller passes those keys.

## 11. Message templates

Owner: Ananda (WhatsApp channel infrastructure) for [scripts/whatsapp/templates.json](../scripts/whatsapp/templates.json). Logical names live in [domains/cold_chain_excursion/messages.json](../domains/cold_chain_excursion/messages.json).

WhatsApp template name: `oerf_<logical_name>`. The twenty names in `templates.json` are the Utility templates. Category `UTILITY`, language `en`.

Notion page 20 sections 3 and 4 (free-form in-session replies, voice script, SMS and email text) do not get a WhatsApp template. Catalog names in that bucket:

- Voice script: `driver_call`.
- In-session free-form: `final_question` (cold-chain policies set it null for the MVP), `evidence_confirm`, `evidence_unreadable`, `concern_reply`, `evidence_resubmitted`, `disambiguation`, `voice_note_unsupported`, `callback_ack`, `reopened_late_reply`, `judge_welcome`, `sandbox_full`.

SMS and email strings are renderings of a logical message, not extra template names. No catalog name sits outside `templates.json` and that bucket.

## 12. Path ownership

No `CODEOWNERS` file is in the repo. This map is the contract until one is added.

| Path | Owner |
| --- | --- |
| `infra/stacks/data_stack.py` | Data-stack owner. Changes only through `docs/change-requests/`. |
| `infra/app.py`, `infra/stacks/compute_stack.py` | Ananda (OERF-301) |
| `oerf/platform/channels/router.py` | Mudita |
| `oerf/platform/report/` | Mudita (OERF-117) |
| `oerf/platform/api/action_handler.py` | Mudita (OERF-208) |
| `oerf/platform/api/ws_handler.py`, `ws_authorizer.py` | Mudita (OERF-302) |
| `oerf/platform/api/stream_broadcaster.py` | Ananda (OERF-308). Stays in `api/` unless OERF-302 agrees to move it. |
| `oerf/platform/evidence/vision.py` | OERF-207 for behaviour. Ananda (OERF-310) for model, guardrail, and IAM scope. |
| `oerf/platform/observability/` | Ananda (OERF-303). Not created yet. |
| `web/**` | Mudita (OERF-302 and the dashboard tickets) |
| `scripts/whatsapp/` | Ananda (WhatsApp channel infrastructure) |
| `docs/interfaces.md` | Ananda (OERF-304), then OERF-306 |
| `.github/workflows/` | DevOps (OERF-313) |
| OERF-112–118, 309, 311 | Mudita |
| OERF-303, 304, 305, 306, 307, 308, 310, 312 | Ananda |

Mudita's other ticket numbers in this table are ownership for coordination. They do not extend this file's sign-off table.

## 13. Decisions recorded with this version

| # | Decision | Owner |
| --- | --- | --- |
| 1 | Resource env vars use `*_TABLE_NAME` and `*_BUCKET_NAME`. | Ananda, 301 (acked) |
| 2 | Role comes from `cognito:groups`. `custom:role` is dropped. | contract |
| 3 | Actions are `POST /incidents/{id}/actions` with `confirm_transfer`, `pick_option`, or `cancel`, plus `action_id`, via SQS. The deployed `/action` verbs are non-conformant. | Mudita, 208/302 |
| 4 | Incident key `TENANT#{tid}#INC#{iid}` is authoritative. The action-handler key is non-conformant. | Mudita, 208 |
| 5 | `states:SendTaskSuccess` belongs only to the inbound handler. | Ananda, 301 |
| 6 | WebSocket scopes are incident (full sanitized event) and tenant list (summary only). No `ALL`. | Ananda 308, Mudita 302 |
| 7 | Simulator `Detail` keeps `tenant_id` and `shipment_id`. Ingest uses `DEVICE#{id}` and drops the event when `detail.tenant_id` disagrees. | OERF-118 and ingest |
| 8 | GSI1 by status is required for `GET /incidents`. Data-stack change request. Dependency of 302. | data-stack owner |
| 9 | Lifecycle source `oerf.incident`, detail-type `IncidentStateChanged`, detail as in section 4.2. Consumer is the stream path. No EventBridge consumer yet. | Ananda, 308 |
| 10 | Page 20 sections 3–4 need no WhatsApp template. The outside list is empty (section 11). | Ananda, WhatsApp infra |
| 11 | OERF-201–207 owner names stay blank until Ananda fills them. | Ananda |

## 14. Open items

1. OERF-201–207 owner names. Blank on purpose.
2. `POST /incidents/trigger` on the action handler starts the placeholder state machine and writes the non-conformant incident key. The frozen ingest path is EventBridge. Owner to confirm whether the route stays as a demo hook: Mudita (208) with Ananda (301).
3. The report function's role has `ses:SendEmail`. ADR-008 puts SES send on the channel router. Mudita (117) confirms the report email path.
4. `custom:tenant_id` is a data-stack change, not a product choice. It is HIGH and unblocked: [change request](change-requests/2026-10-18-cognito-custom-tenant-id.md).
5. GSI1 is a data-stack change, dependency of OERF-302: [change request](change-requests/2026-10-18-incident-gsi1-by-status.md).

## 15. Sign-off request

Post this:

> Interface contract v1.0 is in `docs/interfaces.md`. Please sign the row for your ticket.
>
> Already decided: env vars use `*_TABLE_NAME` / `*_BUCKET_NAME` (301 acked). Role is `cognito:groups`. Dashboard actions are `POST /incidents/{id}/actions` with `confirm_transfer`, `pick_option`, or `cancel` and a client `action_id`, queued to SQS (Mudita 208/302 migrates today's `/action` route). Incident key is `TENANT#{tid}#INC#{iid}` (Mudita 208). `SendTaskSuccess` moves to the inbound handler only (Ananda 301). WebSocket subscriptions are incident (sanitized full event) or tenant list (summary: incident_id, state, stage, version); no ALL (Ananda 308, Mudita 302). Simulator readings keep tenant_id and shipment_id; ingest trusts `DEVICE#{id}` and drops a mismatch. Lifecycle events are `oerf.incident` / `IncidentStateChanged` on the stream path only.
>
> Data-stack owner: two requests. HIGH — add `custom:tenant_id` without replacing the user pool (`docs/change-requests/2026-10-18-cognito-custom-tenant-id.md`). Without it, authorized routes have no tenant. Second — incident GSI1 for `GET /incidents`, which blocks 302.
>
> OERF-201–207 names are still blank. Reply on your row if this matches what you will build.
