# OERF interface contract

**FROZEN v1.23** — authored 2026-10-18 by OERF-304 (Ananda).

Change log:

- v1.23 (2026-10-25) The six cold-chain step functions are `oerf-step-context`, `oerf-step-assess`, `oerf-step-act`, `oerf-step-verify`, `oerf-step-qa`, and `oerf-step-close` in `OerfComputeStack`. `oerf-cold-chain` invokes them for `GatherContext`, `Assess`, `Act`, `VerifyLoop`, `QA`, and `Close`. Every other task in that machine, and every task in `oerf-engage-party`, still invokes `ColdChainWorkflowStub`. Ingest copies a shipment row's `product_profile` onto `workflow.shipment.product_profile_id` when that id is absent. The stored shipment row is unchanged.
- v1.22 (2026-10-25) The HTTP API action route is retired. Section 3.2 places frozen `POST /incidents/{id}/actions` on the OerfApiStack REST API. Section 7.1 lists `GET /incidents` and `GET /incidents/{id}` on `oerf-http-api`. `INBOUND_QUEUE_URL` is set only on the REST dashboard action function. Section 9 resolves the account as context `account`, then `CDK_DEFAULT_ACCOUNT`, then the placeholder `000000000000`, and the region as `CDK_DEFAULT_REGION`, then context `region`, then `us-east-1`.
- v1.21 (2026-10-25) Dashboard `POST /incidents/{id}/actions` is served by the OerfApiStack REST API (`oerf.platform.api.dashboard.handler`). `oerf-http-api` does not expose that route. `oerf-action-handler` reads `GET /incidents` and `GET /incidents/{id}` and does not send to the inbound queue. Its role has no `sqs:SendMessage` on that queue. `INBOUND_QUEUE_URL` is set on the REST dashboard action function.
- v1.20 (2026-10-24) One `READ#` writer: ingest calls `IncidentStore.record_reading` while the shipment lock is present and the incident is not terminal. `PK=TENANT#{tid}#INC#{iid}`, `SK=READ#{ts}#{seq}#{sha256}` where `ts` is `YYYY-MM-DDTHH:MM:SSZ`, `seq` is twelve digits, and `sha256` is the hex SHA-256 of `event_id`. Stored attributes are `pk`, `sk`, `tenant_id`, `ts`, `temperature_c`, and `expires_at`. `expires_at` is the reading `ts` in epoch seconds plus 90 days. Incident-table TTL deletes on `expires_at`. `device_id`, `latitude`, `longitude`, `shipment_id`, and `event_id` are not stored. A conditional put leaves an existing key unchanged. The broadcaster stream filter stays `SK=META`. `EVT#` and `READ#` are not delivered. A record whose `tenant_id` contradicts `TENANT#{tid}#INC#{iid}` is dropped.
- v1.19 (2026-10-23) Ingest writes one temperature row while the shipment lock is present: `PK=TENANT#{tid}#INC#{iid}`, `SK=READ#{ts}#{seq}` with `seq` six digits, attributes `ts` and `temperature_c` only, `expires_at` 90 days from the reading. A conditional put leaves an existing key unchanged. The report temperature section reads those rows. The broadcaster stream filter stays on `SK=META`, so `READ#` is not delivered.
- v1.18 (2026-10-23) Combined with origin/main (PR #30). `DemoDeviceSeed` is created only when context `demo:seedDevices` is true; a normal deploy does not write seed rows. `DeviceSeedFunctionRole` and its X-Ray `Resource: *` exist only in that synth (`docs/security/iam-exceptions.md`). The report generator writes an S3 key only when it is absent (`IfNoneMatch`), pages through every list result, fills the temperature section from `READ#`, and the state timeline from `EVT#`. A missing tenant, a missing incident, or a META `tenant_id` that does not match the event still fails closed. The broadcaster envelope stays the v1.16 shape. The desk rejects another tenant, another incident, and a private field; this broadcaster does not send `shipment_id`, `config_version`, `latest_reading`, `from_state`, `to_state`, or `timeline`.
- v1.17 (2026-10-23) WhatsApp media is stored by End User Messaging `GetWhatsAppMessageMedia`. The handler checks `mimeType` and `fileSize` with `metadataOnly`, then signs a presigned PUT (`headers` empty, no SSE header, 300 seconds) for `tenants/{tenant_id}/inbound/{sha256}/{uuid}{ext}`. The bucket default SSE-KMS `aws/s3` applies. Allowed types are `image/jpeg`, `image/png`, `image/webp`, and `application/pdf`. The size cap is `INBOUND_MEDIA_MAX_BYTES`, default 10 MB. The origination phone id is `WA_PHONE_NUMBER_ID_PARAM` from SSM, or `WHATSAPP_PHONE_NUMBER_ID` when that parameter name is unset. No Meta token is read.
- v1.16 (2026-10-23) Incident-scope `INCIDENT_UPDATE` `data` is `incident_id`, `state`, `stage`, integer `version`, and `updated_at` when the image has one. `incident_id` and `version` match the envelope. `opened_at`, `recommended_option`, and `option_ids` are not sent. `INCIDENT_SUMMARY` is unchanged. The desk allowlist in `web/src/deskEvents.js` accepts this payload.
- v1.15 (2026-10-23) Combined with origin/main (PRs #28 and #29). An unquoted provider reply resumes only when exactly one waiting engagement is stored for that sender; zero or several are ignored. WhatsApp media is downloaded with the Meta Graph API (`https://graph.facebook.com/v21.0/{media_id}`, secret `oerf/whatsapp/access_token`) and stored at `tenants/{tenant_id}/inbound/{sha256}/{uuid}`. Vision writes `evidence_analysis` and `evidence_verification` on incident META. `DemoDeviceSeed` is the only device-seed custom resource and runs on every `OerfComputeStack` deploy. WebSocket `event_type` values stay `INCIDENT_SUMMARY` and `INCIDENT_UPDATE`. `POST /incidents/trigger` stays retired.
- v1.14 (2026-10-23) `POST /incidents/trigger` is retired. The action handler does not call `states:StartExecution`. Ingest (`sim.reefer` on EventBridge) is the only start of an incident and its execution.
- v1.13 (2026-10-23, ADR-019) `cancel` is implemented in the inbound handler. It stops the execution ingest started, sets the incident to `CANCELLED` through the repository, and does not resume a task token. A duplicate action or a `CLOSED` or `CANCELLED` incident does not call `StopExecution` again. `ExecutionDoesNotExist` is already finished and is not an error. Authorised roles are `platform_admin`, `tenant_admin`, and `ops_user`. The queued message carries that token `role`.
- v1.12 (2026-10-23, OERF-308) the broadcaster envelope is `event_type`, `tenant_id`, `incident_id`, `version`, `emitted_at`, and `data`. Incident `data` is `state`, `stage`, `opened_at`, `updated_at`, `recommended_option`, and `option_ids`. Tenant-list `data` is `incident_id`, `state`, `stage`, and `version`. `timeline` and `evidence_analysis` are not sent. A subscription row with no `scope` receives nothing. `REMOVE` is not an event.
- v1.11 (2026-10-22, ADR-019) a `cancel` action is handled by the inbound handler. It stops the `oerf-cold-chain` execution, sets the incident to `CANCELLED`, and the repository releases the lock. That handler logic is not implemented. The action handler has no task-token actions.
- v1.10 (2026-10-22, OERF-303) the logger writes each JSON object to stdout and does not pass the record to a logging handler. A failed route sets `channel` to `none` when no channel was selected.
- v1.9 (2026-10-22, ADR-018) records object retention. Evidence objects expire after 90 days, and noncurrent evidence versions after 30. Report objects stay. Noncurrent report versions expire after 90 days.
- v1.8 (2026-10-22, OERF-303) fills the logger section. A line is JSON with `ts`, `level`, `msg`, and optional `tenant_id`, `incident_id`, `engagement_id`. The placeholder keys `timestamp` and `message` are `ts` and `msg`. Sensitive keys are dropped. E.164 numbers, email addresses, and task-token values are masked.
- v1.7 (2026-10-21, WhatsApp) documents the WABA event destination on the inbound topic. It is created only when context `whatsapp:wabaId` is set.
- v1.6 (2026-10-21, OERF-312) documents the AgentCore placeholder: envelope, timeout, fallback, and `AGENT_RUNTIME_ARN`.
- v1.5 (2026-10-21, OERF-310) sets `GUARDRAIL_ID` and `GUARDRAIL_VERSION` on the vision function, and documents `record_invocation`.
- v1.4 (2026-10-21, OERF-301) records DynamoDB retention. TTL attribute `expires_at` is enabled on the incident and routing tables only. Routing rows that used `ttl` now write `expires_at`. The `ttl` attribute is not used.
- v1.3 (2026-10-21, ADR-020) vision uses `amazon.nova-lite-v1:0`. `USE_MOCK_BEDROCK` defaults to true unless context `channels:live` is true. `BEDROCK_MODEL_ID` comes from context `bedrock:modelId`.

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

Frozen route: `POST /incidents/{id}/actions` on the OerfApiStack REST API (`RestApiUrl`, `oerf.platform.api.dashboard.handler`). Cognito JWT. The body schema rejects unknown fields, so `tenant_id` is not a client field. `oerf-http-api` does not serve this route.

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
  "role": "ops_user",
  "type": "text",
  "text": null,
  "media_ref": null,
  "received_at": "2026-10-18T12:00:00Z"
}
```

Dedupe key: `INMSG#{action_id}` (section 5). An action whose engagement is no longer waiting is ignored and logged, without a task-token call.

Deployed deviation, Mudita (208/302) to migrate: `POST /incidents/{id}/action` accepts `APPROVE`, `REJECT`, and `OVERRIDE`. The handler does not call `SendTaskSuccess`.

`cancel` enters through this queue. The inbound handler stops the execution ingest started (`states:StopExecution` on that machine's executions only). The execution name is the incident id sanitized the same way ingest names `StartExecution`, under `STATE_MACHINE_ARN`. The handler then sets the incident to `CANCELLED` through the repository, which writes the timeline event and releases the shipment lock (ADR-019). It does not call `states:SendTaskSuccess`. A duplicate `action_id`, or an incident already `CLOSED` or `CANCELLED`, does not call `StopExecution` again. `ExecutionDoesNotExist` means the execution is already finished and is not an error. Authorised roles are `platform_admin`, `tenant_admin`, and `ops_user`. Any other role is ignored. The queued `role` is the token role, not a client field. When `engagement_id` is present, the tenant cancelled is that stored engagement's tenant. Otherwise it is the stored incident's tenant. A message `tenant_id` that does not match the stored engagement is ignored.

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

Ingest loads `PK=DEVICE#{device_id}`, `SK=META` on the routing table. That item's tenant and shipment are the ones used to open or update the incident. The event is dropped when `Detail.tenant_id` is present and differs from the registry tenant. An unknown device is dropped. OERF-116 writes the row through `register_device` with `tenant_id`, `shipment_id`, and `asset_id`. Demo rows come from `sim/seed/*/shipments.json` (`device`, `truck`). Ingest only reads the row. The EventBridge rule matches source and detail-type only. Thresholds stay in tenant config.

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

### 4.4 WhatsApp event destination

Owner: Ananda (WhatsApp channel infrastructure).

Context `whatsapp:wabaId` is empty by default. When it is a `waba-...` id, `OerfComputeStack` does two things and otherwise leaves the inbound topic unchanged.

The inbound topic policy allows `social-messaging.amazonaws.com` to call `sns:Publish` on that topic. Both conditions are `StringEquals`: `aws:SourceAccount` is this account, and `aws:SourceArn` is `arn:aws:social-messaging:{region}:{account}:waba/{wabaId}`. That ARN shape is the `waba` resource in the [social-messaging Service Authorization Reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_socialmessaging.html): `arn:${Partition}:social-messaging:${Region}:${Account}:waba/${WabaId}`.

A custom resource calls `PutWhatsAppBusinessAccountEventDestinations` with `id` set to the WABA id and one destination, the topic ARN. Delete calls the same API with `eventDestinations` set to `[]`. The API allows 0 or 1 items, and 0 clears the destination. The physical id is the WABA id. The handler role may call `social-messaging:PutWhatsAppBusinessAccountEventDestinations` and `social-messaging:GetLinkedWhatsAppBusinessAccount` on that WABA ARN, and write its own log group.

The inbound topic is created without a customer-managed KMS key. There is no key policy grant. An AWS owned key has no policy to edit.

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
    "role": { "enum": ["platform_admin", "tenant_admin", "ops_user"] },
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

Owner: inbound handler (`oerf-inbound`). Library pieces that already exist: `oerf.platform.inbound.normalize`, `dedupe`, `dynamo`, and `oerf.platform.inbound.handler`.

Order for a provider message:

1. Dedupe. Duplicate `INMSG#` rows are dropped.
2. Opt-out keywords (`STOP`, `UNSUBSCRIBE`, and the tenant's local equivalents) before correlation. Set opt-in false and stop that contact's engagements.
3. `JOIN` (exact text, case-insensitive) when `PHONE#{e164}` has no item whose sort key is a non-sandbox tenant. A number registered to any non-sandbox tenant is not joined. A repeat `JOIN` returns that judge's existing sandbox shipment. The daily counter is `PK=SANDBOX#JOINS#{date}`, `SK=META`, conditional increment while `count` is below `sandbox.max_joins_per_day` (platform setting, default 20). Over the cap, the reply logical name is `sandbox_full`. Under the cap, the sandbox tenant gains a contact (opted in) and a shipment, and the logical reply is `judge_welcome`. Both replies go out through the channel router.
4. Correlation. `context_msg_id` loads `OUTMSG#{id}`. With no context id, `ADDR#{channel}#{address}` loads open engagements. Exactly one match is used. Zero or several are ignored; the handler does not send a disambiguation reply. The channel router writes that address row, SK `ENG#{tid}#{iid}#{eid}`, when it registers an outbound message, and deletes it when the engagement leaves `WAITING`. The address is the phone or email that was sent; it is not taken from the inbound body.
5. WhatsApp media is stored by End User Messaging `GetWhatsAppMessageMedia`. A `metadataOnly` call must return an allowed `mimeType` (`image/jpeg`, `image/png`, `image/webp`, `application/pdf`) and a `fileSize` at or under `INBOUND_MEDIA_MAX_BYTES` (default 10 MB). The handler then signs a presigned PUT for `tenants/{tenant_id}/inbound/{sha256(message_id)}/{uuid}{ext}` with SigV4 in `us-east-1`, a 300 second expiry, and no extra headers. It does not set server-side encryption; the evidence bucket default SSE-KMS `aws/s3` applies. A second call passes that URL as `destinationS3PresignedUrl`. The handler does not download the bytes and does not read a Meta token. A disallowed type, an oversize file, or an invalid media id is acknowledged and omitted from the callback. Throttling and a 5xx are retried. A missing origination phone id fails closed. The handler then calls `states:SendTaskSuccess` on the current rung token. Classification happens in the engagement Interpret step (ADR-017), not here. Vision later writes `evidence_analysis` and `evidence_verification` on incident META.

Dashboard messages skip steps 2–4 and correlate on `engagement_id`.

The handler resumes only when the resolved sender's `tenant_id` and `contact_id` both match the correlated engagement: a quoted message id cannot cross a tenant boundary, and a shared number stays bound to the party that was contacted. Every duplicate, unknown, late, invalid, or mismatched record is acknowledged and logged without a callback.

`oerf.platform.inbound.handler` covers steps 1 and 2, both halves of step 4, media storage in step 5, and the resume. `JOIN` in step 3 is not in that module yet and remains open.

Dedupe item, agreed by the spec and [oerf/platform/inbound/dedupe.py](../oerf/platform/inbound/dedupe.py):

| Key | Value |
| --- | --- |
| Table | routing |
| PK | `INMSG#{provider_msg_id}` or `INMSG#{action_id}` |
| SK | `META` |
| Retention | 7 days from accept time, not from `received_at`. `expires_at` is a number of epoch seconds (§6.1). |

Correlation keys, agreed by the LLD and [oerf/platform/storage/dynamo.py](../oerf/platform/storage/dynamo.py):

| Key | Item |
| --- | --- |
| `OUTMSG#{provider_msg_id}` | Outbound id → tenant, incident, engagement. SK `ENG#{tid}#{iid}#{eid}`. |
| `ADDR#{channel}#{address}` | Address → open engagements. Same engagement SK. |
| `PHONE#{e164}` | SK `TENANT#{tid}#CONTACT#{cid}`. JOIN guard and correlation fallback (ADR-029). |

## 6. Repository access patterns

Owner: OERF-112 for the repository (Mudita). Data-stack owner for indexes. Authoritative incident key, from the LLD and `incident_pk()`:

`PK=TENANT#{tid}#INC#{iid}`, `SK=META`. Timeline rows: `SK=EVT#{iso-timestamp}#{seq}`. Temperature rows, written by `IncidentStore.record_reading` only while `OPEN_INCIDENT` is present and the incident is not terminal: `SK=READ#{ts}#{seq}#{sha256}`. `ts` is `YYYY-MM-DDTHH:MM:SSZ`. `seq` is twelve digits. `sha256` is the hex SHA-256 of `event_id`. Stored attributes are `pk`, `sk`, `tenant_id`, `ts`, `temperature_c`, and `expires_at` (epoch seconds of `ts` plus 90 days). `device_id`, coordinates, `shipment_id`, and `event_id` are not attributes. A second put of the same key does not replace the row.

Ingest writes the authoritative key. The retired trigger route no longer writes a separate incident item.

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
| 20 | Temperature readings in order | incident | `SK` begins_with `READ#` |

Pattern 12 is not deployed. The incident table has no GSI, and the data-stack unit test asserts that. `GET /incidents` (OERF-302) depends on it. Change request: [2026-10-18-incident-gsi1-by-status.md](change-requests/2026-10-18-incident-gsi1-by-status.md).

### 6.1 DynamoDB retention

`expires_at` is a number of Unix epoch seconds. DynamoDB TTL on that attribute is enabled on the incident table and the routing table, and on no other table. Each table has one TTL attribute. Deletion can lag by hours, so readers still compare `expires_at` with the clock.

| Item | Table | Key | Retention | TTL deletes the row |
| --- | --- | --- | --- | --- |
| `INGEST#` | incident | `PK=INGEST#{event_id}`, `SK=META` | 7 days from accept | yes |
| `READ#` | incident | `PK=TENANT#{tid}#INC#{iid}`, `SK=READ#{ts}#{seq}#{sha256}` | 90 days from the reading `ts`. `expires_at` is epoch seconds of `ts` plus 90 days. Attributes: `pk`, `sk`, `tenant_id`, `ts`, `temperature_c`, `expires_at`. | yes |
| `DEBOUNCE#` | incident | `PK=DEBOUNCE#{tid}#{shipment_id}#{device_id}`, `SK=META` | 10 minutes from the reading | yes |
| `INMSG#` | routing | `PK=INMSG#{id}`, `SK=META` | 7 days from accept, not from `received_at` | yes |
| `OUTMSG#` | routing | `PK=OUTMSG#{provider_msg_id}`, `SK=ENG#{tid}#{iid}#{eid}` | 90 days from registration | yes |
| `ADDR#` | routing | `PK=ADDR#{channel}#{address}`, `SK=ENG#{tid}#{iid}#{eid}` | until the engagement leaves `WAITING` | no. `expires_at` is not written; the row is deleted then. |
| `SBXSESSION` | tenant | `PK=TENANT#sbx`, `SK=SBXSESSION#{e164}` | 24 hours in the application | no. The row stays so an expired or opted-out session can still be read. |
| `WS#` | routing | `PK=WS#{connection_id}`, `SK=METADATA` | 24 hours from the write | yes |
| `WS_SUB#` | routing | `PK=WS_SUB#{tid}`, `SK=CONN#{connection_id}` | 24 hours from the subscribe write | yes |
| `MSG#` | routing | `PK=TENANT#{tid}#INCIDENT#{iid}`, `SK=MSG#{message_id}` | 7 days from the send record | yes |
| `ACTIVE_TASK_TOKEN` | routing | `PK=TENANT#{tid}#INCIDENT#{iid}`, `SK=ACTIVE_TASK_TOKEN` | 7 days from the send record | yes |
| `MSG_CORRELATION#` | routing | `PK=MSG_CORRELATION#{message_id}`, `SK=TOKEN` | 7 days from the send record | yes |

These items are not written with `expires_at` and must not expire: incident `SK=META`, timeline `EVT#`, the `OPEN_INCIDENT` lock, the engagement item `SK=ENG#{eid}` on the incident table, `CONTACTENG#`, and tenant config rows (`CONFIG#`, `CADENCE#`, `COMMS#`, `CALENDAR#`, contacts, facilities, fault codes, shipments, audit).

No item writes an attribute named `ttl`. Expiry is `expires_at`, a number of epoch seconds.

S3 prefixes start with `tenants/{tid}/`.

### 6.2 Object storage retention

Evidence objects expire 90 days after they are written. A noncurrent evidence version expires 30 days after it is replaced. Report objects are the audit record and are not expired. A noncurrent report version expires 90 days after it is replaced. Both buckets keep versioning on.

## 7. API and WebSocket events

Owner: Mudita (OERF-302) for routes and the subscribe message. Ananda (OERF-308) for the broadcast payload. Both sign this section.

### 7.1 HTTP API

API `oerf-http-api`. Cognito JWT authorizer, identity source `Authorization`. CORS allows `Content-Type` and `Authorization`.

| Route | Contract |
| --- | --- |
| `GET /incidents/{id}` | Incident META for the token tenant and path id: `incident_id`, `tenant_id`, `shipment_id`, `state`, `reason`, `opened_at`, `config_version`. `config_snapshot` is included on this authenticated read. `pk` and `sk` stay off the wire. |
| `GET /incidents` | List for the token tenant. Blocked on GSI1 (pattern 12). Each row is the summary object in section 7.3. |

Dashboard `POST /incidents/{id}/actions` is section 3.2 on the REST API. Ingest (`sim.reefer` / `TemperatureReading`) is the only way an incident and its execution start.

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

`version` is the integer `version` on incident META when that attribute exists, otherwise `event_seq`, otherwise `1`. `stage` is `stage` when that attribute is set, otherwise `current_stage`, otherwise `state`. `emitted_at` is the time the broadcaster built the message.

The envelope is exactly these keys. `scope` is not a field. The subscription row chooses which `data` object is sent. Adding a `data` key is a minor version of this contract.

Incident scope (`event_type` `INCIDENT_UPDATE`). `data` is `incident_id`, `state`, `stage`, and integer `version`, plus `updated_at` when the image has one. `incident_id` is the same value as the envelope. `version` is the same integer as the envelope. `state` and `stage` are always present. `updated_at` is copied only when it is `YYYY-MM-DDTHH:MM:SSZ` or a non-negative integer epoch second. `opened_at`, `recommended_option`, and `option_ids` are not sent. The desk also allows `config_version`, `from_state`, `latest_reading`, `shipment_id`, `timeline`, and `to_state`; this broadcaster does not send them.

```json
{
  "event_type": "INCIDENT_UPDATE",
  "tenant_id": "t1",
  "incident_id": "INC-000123",
  "version": 4,
  "emitted_at": "2026-10-18T12:00:00Z",
  "data": {
    "incident_id": "INC-000123",
    "state": "VERIFYING",
    "stage": "verify",
    "updated_at": "2026-10-18T12:00:00Z",
    "version": 4
  }
}
```

Tenant-list scope (`event_type` `INCIDENT_SUMMARY`). `data` is exactly these four fields:

```json
{
  "event_type": "INCIDENT_SUMMARY",
  "tenant_id": "t1",
  "incident_id": "INC-000123",
  "version": 4,
  "emitted_at": "2026-10-18T12:00:00Z",
  "data": {
    "incident_id": "INC-000123",
    "state": "VERIFYING",
    "stage": "verify",
    "version": 4
  }
}
```

Neither payload carries a raw DynamoDB image, `opened_at`, `recommended_option`, `option_ids`, `timeline`, `evidence_analysis`, message text, phone numbers, names, task tokens, prompts, images, or model output. A record whose key is not `TENANT#{tid}#INC#{id}` / `META`, or that has no tenant id and incident id, is skipped. `REMOVE` is skipped. This contract has no removed event. A connection subscribed to tenant A never receives tenant B. An incident subscription receives only that incident's `INCIDENT_UPDATE`. A row whose `scope` is missing, `ALL`, or `*` receives nothing. A `scope` of `tenant` is the tenant list and receives `INCIDENT_SUMMARY` only.

## 8. Handler entry points

Owner of the table: OERF-304. Each row's owner owns the module. All of these functions, once deployed, live in `OerfComputeStack`. One role per function. Do not add a second function for a row that already exists.

IAM column is the target. Current wildcards are called out and are not the target.

| Module | Function | Owner | Trigger | IAM target | Status |
| --- | --- | --- | --- | --- | --- |
| `domains.cold_chain_excursion.steps.context` | `handler` | pending (OERF-201–207) | Step Functions `GatherContext` | Read tenant and incident for the execution's tenant. No messaging, no task token. | `oerf-step-context` |
| `domains.cold_chain_excursion.steps.assess` | `handler` | pending (OERF-201–207) | Step Functions `Assess` | Read incident and config snapshot. Write `OPTION#` items. No messaging. | `oerf-step-assess` |
| `domains.cold_chain_excursion.steps.act` | `handler` | pending (OERF-201–207) | Step Functions `Act` | Read/write incident state for act transitions. Sends go through the router, not from this role. | `oerf-step-act` |
| `domains.cold_chain_excursion.steps.verify` | `handler` | pending (OERF-201–207) | Step Functions `VerifyLoop` | Read incident and readings. Write containment state. | `oerf-step-verify` |
| `domains.cold_chain_excursion.steps.qa` | `handler` | pending (OERF-201–207) | Step Functions `QA` | Read incident and report pointer. No SES. | `oerf-step-qa` |
| `domains.cold_chain_excursion.steps.close` | `handler` | pending (OERF-201–207) | Step Functions `Close` | Write `CLOSED`, delete the lock item. No messaging. | `oerf-step-close` |
| `oerf.platform.ingest.handler` | `handler` | pending (OERF-201–207) | EventBridge `sim.reefer` / `TemperatureReading` | Read routing device item and tenant config. Transact incident META plus lock. `states:StartExecution` on `STATE_MACHINE_ARN` only. | not deployed |

`oerf-cold-chain` invokes the six functions above by fixed name: `GatherContext`, `Assess`, `Act`, `VerifyLoop` (payload step `Verify`), `QA`, and `Close`. The data stack writes `arn:aws:lambda:{region}:{account}:function:{name}` into the definition and does not reference `OerfComputeStack`. Every other task in `oerf-cold-chain`, and every task in `oerf-engage-party`, still invokes `ColdChainWorkflowStub`.

The execution input's `workflow.shipment` is a copy of the tenant shipment row. When that row has `product_profile` and no `product_profile_id`, ingest sets `product_profile_id` to the profile value. `PH-2841` stores `product_profile` `PP-2-8-45`, so GatherContext receives `product_profile_id` `PP-2-8-45`. The stored row is not rewritten.
| `oerf.platform.inbound.handler` | `handler` | pending (OERF-201–207) | SQS inbound queue | Read/write routing and incident. Write evidence objects. `states:SendTaskSuccess` only; section 5 keeps reply classification in Interpret, so this role sends no `states:SendTaskFailure`. `states:StopExecution` on executions of `oerf-cold-chain` only. No channel-send APIs. | not deployed |
| `oerf.platform.channels.router` | `handler` | Mudita | Step Functions / direct invoke | Read/write routing (`OUTMSG#`, `ADDR#`). The only role with WhatsApp, SMS, and SES send. | deployed `oerf-channel-router` |
| `oerf.platform.report.generator` | `handler` | Mudita (OERF-117) | Step Functions | Read incident META, `EVT#`, and `READ#`. Write `REPORT_BUCKET_NAME` only when the key is absent. SES send is not the target (ADR-008). | deployed `oerf-report-generator` |
| `oerf.platform.api.stream_broadcaster` | `handler` | Ananda (OERF-308) | Incident table stream | Read incident stream. Read routing subscriptions. Delete gone connections. `execute-api:ManageConnections` on this API. No Step Functions, no incident writes. | deployed `oerf-stream-broadcaster` |
| `oerf.platform.api.ws_handler` | `handler` | Mudita (OERF-302) | `$connect`, `$disconnect`, `subscribe` | Read/write routing connection items only. | deployed `oerf-ws-handler` |
| `oerf.platform.api.ws_authorizer` | `handler` | Mudita (OERF-302) | WebSocket `$connect` authorizer | `cognito-idp:GetUser`. Write the connection binding. | deployed `oerf-ws-authorizer` |
| `oerf.platform.api.action_handler` | `handler` | Mudita (OERF-208) | HTTP API | Read token claims. Read incident for `GET`. No inbound-queue send. No `states:SendTaskSuccess`. | deployed `oerf-action-handler` |
| `oerf.platform.evidence.vision` | `handler` | OERF-207 (behaviour, name pending). Ananda (OERF-310) for model and guardrail env. | Step Functions / async invoke | Read evidence object. Write the evidence result on the incident. `bedrock:InvokeModel` on the approved model ARN. `bedrock:ApplyGuardrail` on the guardrail ARN and on `us.guardrail.v1:0` in us-east-1, us-east-2, and us-west-2. | deployed `oerf-evidence-analyzer` |
| AgentCore runtime | `oerf/platform/agent/agentcore/app.py` | OERF-206 behaviour. Ananda (OERF-312) for the runtime. | Invoke from Interpret | Read incident and tenant tables. `bedrock:InvokeModel` on Nova Lite. `bedrock:ApplyGuardrail` on the guardrail ARN and on `us.guardrail.v1:0` in us-east-1, us-east-2, and us-west-2. Pull the CDK asset repository. No incident writes, no channel send, no task token. | synth when context `agentcore:enabled` is true; default false |

Current IAM that the target replaces:

- Channel router: `ses:SendEmail`, `sns:Publish`, `socialmessaging:*` on `*`.
- Evidence analyzer: `bedrock:InvokeModel` on the approved foundation-model ARN. `bedrock:ApplyGuardrail` on the guardrail ARN and on `us.guardrail.v1:0` in us-east-1, us-east-2, and us-west-2.
- Report generator: `ses:SendEmail` on `*`.
- Action handler: no `states:SendTaskSuccess` or `states:SendTaskFailure`. The inbound role holds `states:SendTaskSuccess` (ADR-028). A decline or an unusable reply travels through the engagement decision branch (section 5).

`states:SendTaskSuccess` is not resource-scopable. The wildcard resource stays, limited to the inbound role, and is recorded in `docs/security/iam-exceptions.md` when that file exists. `states:SendTaskFailure` is not granted to the inbound role.

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
| `STATE_MACHINE_ARN` | Inbound and ingest. Ingest uses it for `StartExecution`. Inbound uses it to derive the execution ARN for `StopExecution`. | compute stack |
| `INBOUND_QUEUE_URL` | Inbound queue URL. Set on the OerfApiStack dashboard action function, which sends the dashboard action. Not set on `oerf-action-handler`. | OERF-301 |
| `CONFIG_ROOT` | directory of domain packages inside the asset. Value `domains`. The config library currently resolves the installed `domains` package and does not read this variable. | OERF-301, when a function needs an explicit root |
| `LOG_LEVEL` | logging level name. Default `INFO` when unset. | OERF-301 / OERF-303 |
| `USER_POOL_ID` | deployed on the WebSocket authorizer | compute stack |
| `USER_POOL_CLIENT_ID` | deployed on the WebSocket authorizer | compute stack |
| `WEBSOCKET_ENDPOINT` | deployed on the broadcaster | compute stack |
| `WHATSAPP_PHONE_NUMBER_ID` | Router and inbound. Value is context `whatsapp:phoneNumberId`, default empty. Used only when `WA_PHONE_NUMBER_ID_PARAM` is unset. | WhatsApp infra |
| `SES_SOURCE_EMAIL` | deployed on the router and the report function | compute stack |
| `USE_MOCK_WHATSAPP` | Router. Default `true`. `false` only when context `channels:live` is true. | WhatsApp infra |
| `USE_MOCK_SMS` | Router. Same default as `USE_MOCK_WHATSAPP`. | WhatsApp infra |
| `USE_MOCK_EMAIL` | Router. Same default as `USE_MOCK_WHATSAPP`. | WhatsApp infra |
| `USE_MOCK_BEDROCK` | Vision. Default `true`. `false` only when context `channels:live` is true. | ADR-020 |
| `BEDROCK_MODEL_ID` | Vision. Context `bedrock:modelId`, default `amazon.nova-lite-v1:0`. | ADR-020 |
| `WA_PHONE_NUMBER_ID_PARAM` | Router and inbound. Value `/oerf/whatsapp/phone-number-id`. The handler reads that SSM parameter once per process. An empty or failed read does not fall back to `WHATSAPP_PHONE_NUMBER_ID`. | WhatsApp infra |
| `INBOUND_MEDIA_MAX_BYTES` | Inbound. Default `10485760` (10 MB). WhatsApp `fileSize` above this is rejected. | compute stack |
| `GUARDRAIL_ID` | Vision only. Id of the guardrail in `OerfComputeStack`. | OERF-310 |
| `GUARDRAIL_VERSION` | Vision only. Published version of that guardrail. | OERF-310 |
| `AGENT_RUNTIME_ARN` | Value of stack output `AgentRuntimeArn` when context `agentcore:enabled` is true. Cross-stack export name is `AGENT-RUNTIME-ARN` because an export name cannot contain underscores. Not set on a function. Default synth leaves it unset. | OERF-312 |

`GUARDRAIL_ID` and `GUARDRAIL_VERSION` are set on `oerf-evidence-analyzer` only. Values come from `infra/config/guardrail.json`. Content filters use the Standard tier at high strength. That tier requires the US guardrail profile `us.guardrail.v1:0`, which the stack passes as `arn:aws:bedrock:{region}:{account}:guardrail-profile/us.guardrail.v1:0`. Denied topics are a product release or rejection, and a medical or safety claim. Phone, email, and name are anonymized on input and output. The vision role may call `bedrock:ApplyGuardrail` on that guardrail ARN and on `arn:aws:bedrock:{region}:{account}:guardrail-profile/us.guardrail.v1:0` for `region` in us-east-1, us-east-2, and us-west-2. It may call `bedrock:InvokeModel` on the Nova Lite foundation-model ARN. Those three profile ARNs are the destination regions for source us-east-1 in the [guardrail profile permissions guide](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrail-profiles-permissions.html).

The live Converse call sends `guardrailConfig` with `trace` set to `disabled` when `GUARDRAIL_ID` is set. The mock path does not. The [Converse guardrail guide](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-converse-api.html) evaluates message text and `guardContent`. It does not list the model `image` content block. Image filters are off by default and, in the [image-filter guide](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-mmfilter.html), are shown on ApplyGuardrail and on image-generation InvokeModel. This guardrail leaves the image modality unset. Vision may still send a WebP image, which those filters do not accept.

### 9.1 Agent runtime

Context `agentcore:enabled` defaults to false so synth does not build an image. When it is true, `OerfComputeStack` adds an AgentCore runtime (`aws_cdk.aws_bedrockagentcore.Runtime` and `CfnRuntime` underneath). The image context is [oerf/platform/agent/agentcore/](../oerf/platform/agent/agentcore/). `oerf/platform/agent/runtime.py` stays the step composition root.

The placeholder listens on port 8080. `POST /invocations` reads a JSON object with `tenant_id`, `incident_id`, `task`, `inputs`, and `trace_id`. It returns `{"status": "placeholder", "fallback": true}` and does not echo those fields. `GET /ping` returns `{"status": "Healthy"}`. The docstring on `handle` is `placeholder, behaviour owned by OERF-206`.

The runtime idle timeout is 60 seconds and the max lifetime is 5 minutes. The network mode is public. The execution role may pull the image, write its log group, call X-Ray, invoke Nova Lite, apply the guardrail, and read the incident and tenant tables. It cannot write those tables or call Step Functions, WhatsApp, SMS, SES, or SNS.

Section 8 does not name a caller role for OERF-206. The stack adds placeholder role `oerf-agent-caller`, trusted by `lambda.amazonaws.com`, with `bedrock-agentcore:InvokeAgentRuntime` on the runtime ARN only. OERF-206 replaces that role when it names the real caller.

The image is published to the CDK asset repository. The execution role pulls that repository: `ecr:BatchGetImage` and `ecr:GetDownloadUrlForLayer` on its ARN. `ecr:GetAuthorizationToken` is `*` because ECR authorization has no resource type.

Provider secrets stay in Secrets Manager, referenced by name or ARN. They are not environment variables.

Synth: [infra/app.py](../infra/app.py) takes the account from context `account`, then `CDK_DEFAULT_ACCOUNT`, then the placeholder `000000000000`. A CloudFormation rule rejects a deploy of that placeholder. Region resolves `CDK_DEFAULT_REGION`, then context `region`, then `us-east-1`.

## 10. Logger

Owner: OERF-303. Module: [oerf/platform/observability/logging.py](../oerf/platform/observability/logging.py). `get_logger(name)` returns a stdlib logger with a redaction filter. `log(logger, level, msg, **fields)` writes one JSON object to stdout with `sys.stdout.write` and a flush. The filter does not pass the record to a logging handler, so the CloudWatch log event is that object. A failed channel-router line sets `channel` to `none` when the route has no channel.

`ts` is UTC `YYYY-MM-DDTHH:MM:SSZ`. `level` and `msg` are always present. `tenant_id`, `incident_id`, and `engagement_id` are omitted when the caller does not pass them. `channel`, `outcome`, and `count` are the only other fields kept.

The filter drops `task_token`, `body`, `text`, `message`, `phone`, `to`, `recipient`, `e164`, `email`, `name`, `address`, and `authorization`. It masks an E.164 number, an email address, or a task-token-shaped value inside `msg` and the fields that remain. A caller that passes a prompt, an image, raw model output, or a raw DynamoDB image gets the same treatment: the key is dropped, and the value is not written.

```json
{
  "ts": "2026-10-18T12:00:00Z",
  "level": "INFO",
  "msg": "route_complete",
  "tenant_id": "t1",
  "incident_id": "INC-000123",
  "engagement_id": "ENG-03"
}
```

The channel router (including mock sends), the vision handler, and the report handler use this logger. They log ids, channel, outcome, and counts.

### 10.1 Model-call trace

Owner: Ananda (OERF-310). Module: [oerf/platform/observability/ai_trace.py](../oerf/platform/observability/ai_trace.py). OERF-206 and OERF-207 call `record_invocation` from their own handlers. This contract does not wire it into a handler owned by someone else.

The mapping has exactly `model_id`, `latency_ms`, `input_tokens`, `output_tokens`, `guardrail_action`, and `outcome`. With an open X-Ray segment, each one is an annotation. `record_invocation` raises `TraceRejected` for any other field, including a prompt, an image, raw model output, a phone number, an email address, or a name. `guardrail_action` and `outcome` are short tokens such as `NONE`, `BLOCKED`, `ANONYMIZED`, `SUCCESS`, or `ERROR`. The guardrail trace is not an argument.

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
| `oerf/platform/observability/ai_trace.py` | Ananda (OERF-310). `logging.py` is OERF-303. |
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
| 4 | Incident key `TENANT#{tid}#INC#{iid}` is authoritative. Ingest writes it. | Mudita, 208 |
| 5 | `states:SendTaskSuccess` belongs only to the inbound handler. | Ananda, 301 |
| 6 | WebSocket scopes are incident (full sanitized event) and tenant list (summary only). No `ALL`. | Ananda 308, Mudita 302 |
| 7 | Simulator `Detail` keeps `tenant_id` and `shipment_id`. Ingest uses `DEVICE#{id}` and drops the event when `detail.tenant_id` disagrees. | OERF-118 and ingest |
| 8 | GSI1 by status is required for `GET /incidents`. Data-stack change request. Dependency of 302. | data-stack owner |
| 9 | Lifecycle source `oerf.incident`, detail-type `IncidentStateChanged`, detail as in section 4.2. Consumer is the stream path. No EventBridge consumer yet. | Ananda, 308 |
| 10 | Page 20 sections 3–4 need no WhatsApp template. The outside list is empty (section 11). | Ananda, WhatsApp infra |
| 11 | OERF-201–207 owner names stay blank until Ananda fills them. | Ananda |

## 14. Open items

1. OERF-201–207 owner names. Blank on purpose.
2. Retired in v1.14. `POST /incidents/trigger` is removed. Ingest is the only start path.
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
