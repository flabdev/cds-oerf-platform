# CDS OERF Platform

Operational Exception Resolution Framework for a cold-chain shipment. A medicine truck must stay inside a temperature band. When it does not, the platform opens one incident for that shipment, ranks recovery options, and accepts inbound replies without handling the same message twice.

The pieces below are libraries and a data stack. No Lambda or Step Functions workflow calls them yet. Persistence that will live in DynamoDB also has an in-memory implementation so tests can run without AWS. The running service will use the DynamoDB adapters; the in-memory classes stay as test doubles.

## Flow

Two paths meet on the incident.

```text
Temperature path
  sim/temperature.py
    -> rules/trigger.py          (config + product profile + debounce streak)
    -> incidents/debounce.py     (streak per tenant, shipment, and device)
    -> incidents/repository.py   (open one incident)
    -> incidents/lock.py         (one open incident per shipment)
    -> rules/allowance.py        (minutes left above the limit)
    -> rules/options.py          (rank facilities against that allowance)

Reply path
  inbound/normalize.py           (WhatsApp payload -> one message shape)
    -> inbound/dedupe.py         (ignore the same provider id for 7 days)
    -> channels/optout.py        (STOP / UNSUBSCRIBE before correlation)
    -> channels/engagement.py    (one lookup to the waiting engagement)
    -> channels/registry.py      (phone number -> tenant and contact)
```

1. The simulator builds a fixed series of `TemperatureReading` events for a tenant and shipment. It prints JSON. It does not publish to EventBridge yet.
2. The trigger reads the resolved tenant config. For T1 the safe band is 2–8°C and two consecutive breaches are required. A reading exactly on 8°C is still safe.
3. Debounce stores that streak for 10 minutes. A later in-range reading clears it. The trigger function itself does not store the streak; the caller passes the count in.
4. The first trigger that should open a case calls `create`. That writes the incident in state `OPEN` and takes the shipment lock (`TENANT#{tenant}#SHIP#{shipment}` / `OPEN_INCIDENT`). A second create for the same shipment appends a timeline row to the incident already locked.
5. Allowance and option ranking are pure functions used when the incident is assessed. They do not read telemetry. The caller supplies minutes already used, the current temperature, and facility drive times from the seed.
6. Legal state changes go through `transition`. `CLOSED` and `CANCELLED` release the shipment lock so a later excursion can open a new incident.
7. An inbound WhatsApp payload is normalized to channel `whatsapp`, then deduped on the provider message id. A message whose whole text is `STOP` or `UNSUBSCRIBE` sets that contact's opt-in to false and stops active engagements. It does not resume the waiting workflow. Any other reply looks up `OUTMSG#{provider message id}` and resumes only while that engagement is still `WAITING`.
8. The simulator's scenario command writes PutEvents-shaped temperature JSON. `sim/responder.py` returns the scripted reply for a scenario, role, ask, and attempt. Neither one publishes to AWS.

Closing or cancelling is the only step that removes the lock. A repeated warm reading while the lock is held updates the open incident.

## Where code lives

Application code is under `oerf/platform/` because a top-level package named `platform` would shadow the Python standard library. Domain JSON, simulator seed, and CDK stay at the repository root. The root `platform/` directory only holds `.gitkeep` placeholders from the original layout.

## Application files

### Tenancy

| File | Role |
| --- | --- |
| `oerf/platform/tenancy/context.py` | `TenantContext`. Rejects a blank tenant id. No I/O. |
| `oerf/platform/tenancy/config_resolver.py` | Loads domain defaults, deep-merges the tenant override, validates with the JSON Schema, and returns `ResolvedConfig`. |
| `oerf/platform/tenancy/dynamo_config.py` | Same resolution from the tenant table. Packaged JSON remains the schema and defaults. Reads the ACTIVE pointer, then the version row it names. A `v1` row is readable when no ACTIVE pointer exists. |
| `oerf/platform/tenancy/publisher.py` | Saves a draft, validates it, and publishes an immutable version. The ACTIVE pointer moves. An open incident keeps the snapshot it was given at create. |
| `oerf/platform/tenancy/__init__.py` | Exports `get_tenant_context`, `get_config`, and the config errors. |

### Rules

| File | Role |
| --- | --- |
| `oerf/platform/rules/allowance.py` | Turns configured allowance and elapsed minutes into remaining time and a status: within, exhausted, or exceeded. |
| `oerf/platform/rules/options.py` | Projects each recovery option (drive, unload, rise, cooldown) and ranks feasible options by spare minutes, then shorter drive. |
| `oerf/platform/rules/trigger.py` | Decides whether one reading is a breach and whether the streak has reached `debounce_readings`. |
| `oerf/platform/rules/__init__.py` | Exports the three rule functions and their result types. |

### Cadence

| File | Role |
| --- | --- |
| `oerf/platform/cadence/policy.py` | Parses ISO-8601 durations and loads the cold-chain ladders. Policy choice is `role.ask` from data. |
| `oerf/platform/cadence/calendar.py` | Quiet hours. A rung that lands inside them is deferred, not skipped. |
| `oerf/platform/cadence/engine.py` | `compute_next_rung`. Pure: the caller passes `now`, history, calendar, and multiplier. Voice does not wait for a spoken reply. A callback pause does not move the exhaustion deadline. |
| `oerf/platform/cadence/__init__.py` | Exports the cadence functions. |
| `domains/cold_chain_excursion/cadence.json` | Driver, facility, and ops ladders. Times below are the `after` values on those rungs. |
| `domains/cold_chain_excursion/cadence_schema.json` | Schema for that ladder file. |

`compute_next_rung` does not send a message. The caller passes the ladder, the messages already sent, and the current time. The answer is the next channel and when it is due, or a stop. Times are measured from the start of that conversation. A reply is handled by the caller, which stops asking for the next step. Quiet hours and a callback pause delay the next step; they do not skip it or move the stop time. The demo clock multiplier shortens only the real wait (`real_seconds`). The due time stays on the scenario clock.

The role and question pick the ladder from `defaults.json`: `driver.diagnose` → `critical_field`, `facility.accept` → `partner_request`, `ops_manager.approve` → `approval_fast`.

Driver (`critical_field`). Quiet hours are ignored. Stop at 4 minutes and continue with the cause recorded as unknown.

| Due | Channel | Message |
| --- | --- | --- |
| Immediately | WhatsApp | Check-in |
| 1 minute | WhatsApp | Reminder |
| 1 minute 30 seconds | SMS | Reminder |
| 2 minutes | Voice | Call. A reply is still expected on WhatsApp. |
| 3 minutes | WhatsApp | Escalate to the fleet supervisor |

Facility (`partner_request`). Quiet hours apply. Stop at 5 minutes and try the next facility.

| Due | Channel | Message |
| --- | --- | --- |
| Immediately | WhatsApp | Request |
| 1 minute 30 seconds | WhatsApp | Reminder |
| 2 minutes 30 seconds | SMS | Reminder |
| 3 minutes | WhatsApp | Escalate to the ops manager |

Ops manager (`approval_fast`). Quiet hours apply. Stop at 2 minutes, then auto-proceed or escalate.

| Due | Channel | Message |
| --- | --- | --- |
| Immediately | WhatsApp | Approval request |
| 1 minute | WhatsApp | Reminder |
| 1 minute 30 seconds | Email | Reminder |

### Incidents

| File | Role |
| --- | --- |
| `oerf/platform/incidents/repository.py` | In-memory incident store and the `IncidentStore` interface. Creates `OPEN` incidents, appends timeline events, and allows only the specification's state edges. |
| `oerf/platform/incidents/lock.py` | In-memory shipment lock and the `OpenIncidentLockPort` interface. The first acquire wins. |
| `oerf/platform/incidents/debounce.py` | In-memory and DynamoDB streak counters. `observe` records a breach or clears the streak when the reading is back in range. |
| `oerf/platform/incidents/dynamo.py` | DynamoDB incident store and shipment lock. Create writes the incident, the first timeline row, and the lock in one transaction. Close and cancel delete the lock in the transition transaction. |

### Inbound messages

| File | Role |
| --- | --- |
| `oerf/platform/inbound/normalize.py` | Turns a WhatsApp payload into one message: provider id, channel `whatsapp`, sender, time, text, and media. It does not classify the text. |
| `oerf/platform/inbound/dedupe.py` | In-memory dedupe and the `MessageDedupePort` interface. Keeps a provider message id for 7 days from the injected clock. |
| `oerf/platform/inbound/dynamo.py` | DynamoDB dedupe on `INMSG#{message id}` / `META`. A retry inside the window does not write again. |

### Engagements

| File | Role |
| --- | --- |
| `oerf/platform/channels/engagement.py` | Waiting engagement on `ENG#{id}`, plus the `OUTMSG#{provider message id}` row a reply quotes. A stopped, answered, or expired row does not resume. |
| `oerf/platform/channels/registry.py` | `PHONE#{e164}` returns every tenant and contact registered on that number. |
| `oerf/platform/channels/optout.py` | `STOP` and `UNSUBSCRIBE`, plus configured local keywords, run before correlation. |

### Storage helpers

| File | Role |
| --- | --- |
| `oerf/platform/storage/dynamo.py` | Key names, attribute helpers, and detection of a conditional DynamoDB failure. It does not create a client. |
| `oerf/platform/storage/__init__.py` | Package marker. |

Package markers with no behavior: `oerf/__init__.py`, `oerf/platform/__init__.py`.

## Domain and seed data

| File | Role |
| --- | --- |
| `domains/cold_chain_excursion/schema.json` | JSON Schema for a cold-chain config. Unknown fields are rejected. |
| `domains/cold_chain_excursion/defaults.json` | Published defaults: profile `PP-2-8-45` (2–8°C, 45 minutes), debounce 2, assessment rates. |
| `domains/cold_chain_excursion/messages.json` | Message catalog. Only `driver_checkin` has SMS text. |
| `domains/__init__.py` | Makes `domains` an importable package so the JSON ships with the install. |
| `sim/seed/load.py` | Loads a tenant seed directory. Repeatable, no AWS. |
| `sim/seed/t1/tenant.json` | Tenant `t1`, timezone `Asia/Kolkata`, demo clock multiplier 20. |
| `sim/seed/t1/domains/cold_chain_excursion.json` | T1 override, including profile `PP-15-25-120`. |
| `sim/seed/t1/shipments.json` | Demo shipment `PH-2841`, device `RF-7781`, profile `PP-2-8-45`. |
| `sim/seed/t1/facilities.json` | Recovery facilities A and B with drive minutes used by option ranking. |
| `sim/seed/t1/contacts.json` | Driver, ops, QA, and facility contacts. Phone numbers are tokens, not real numbers. |
| `sim/seed/t2/` | Tenant `t2`, brand Sample Vaccine 3PL, shipment `VX-3101`, 45-minute seed allowance, approval ladder `PT1M`, scripted contacts. |
| `sim/seed/sbx/` | Sandbox tenant, demo clock, shipment `SBX-0001`, scripted contacts. |
| `sim/__init__.py`, `sim/seed/__init__.py` | Package markers. |

`get_config` deep-merges `defaults.json` with the tenant file. Nested objects merge. Lists and scalars in the tenant file replace the default.

### Published versions

After two publishes, one tenant and domain have three rows. The partition key is `TENANT#{tenant}`.

| Sort key | What it holds |
| --- | --- |
| `CONFIG#cold_chain_excursion#v1` | The full config for version 1. Never updated. |
| `CONFIG#cold_chain_excursion#v2` | The full config for version 2. Never updated. |
| `CONFIG#cold_chain_excursion#ACTIVE` | `version` (the latest, here `2`) and `previous_version` (only the one before it, here `1`). No full config. |

A normal read loads `ACTIVE`, takes the number in `version`, and then loads that version row. There is no key named `ACTIVEv2`. A later publish adds `v3` and overwrites the same `ACTIVE` row so `version` is `3` and `previous_version` is `2`. Older version rows stay. An incident stores a copy of the config it opened with, so a later publish does not change it.

## Simulator

| File | Role |
| --- | --- |
| `sim/temperature.py` | Builds deterministic readings for `normal` (5.2°C), `excursion` (climbs to 8.1°C), and `recovery` (8.6°C back to 5.0°C). Each record uses `Source`, `DetailType`, `EventBusName`, `Time`, and a `Detail` object that holds the tenant, shipment, device, time, temperature, coordinates, and sequence. The CLI prints the JSON. `--dry-run` is accepted; both modes print until a publisher exists. |
| `sim/scenarios.py` | Named scenario curves, including the happy path, a facility decline, a one-reading blip, and a false alarm. `--emit eventbridge` writes that JSON. The start time is an argument. |
| `sim/responder.py` | Deterministic reply for a scenario, role, ask, and attempt. A silent party returns no reply. No provider call. |

## Infrastructure

| File | Role |
| --- | --- |
| `infra/app.py` | CDK app entry. Requires a resolved AWS account and registers the data stack. |
| `infra/stacks/data_stack.py` | Platform, tenant, incident, and routing tables; evidence and report buckets; event bus `oerf-events`; a placeholder cold-chain state machine; Cognito groups `platform_admin`, `tenant_admin`, and `ops_user`. |
| `cdk.json` | CDK toolkit config. |
| `infra/__init__.py`, `infra/stacks/__init__.py` | Package markers. |

The incident logic does not call these AWS resources yet. The DynamoDB adapters expect an injected client aimed at the incident, routing, and platform tables.

## Tests

| File | Role |
| --- | --- |
| `tests/unit/tenancy/test_context.py` | Tenant id validation. |
| `tests/unit/tenancy/test_config_resolver.py` | Merge, schema rejection, and packaged JSON loading. |
| `tests/unit/tenancy/test_dynamo_config.py` | A stored v1 row resolves, and tenant overrides replace only the fields they set. |
| `tests/unit/tenancy/test_publisher.py` | Invalid drafts cannot publish. T2 at 45 minutes makes Facility B feasible; publishing 30 minutes makes it infeasible. An incident opened on version 1 stays on version 1. |
| `tests/unit/cadence/test_engine.py` | Ladders, quiet hours, clock multiplier, callback pause, and rejected durations. |
| `tests/unit/seed/test_t1_seed.py` | T1 seed matches the specification's demo values. |
| `tests/unit/rules/test_allowance.py` | Remaining minutes and the three statuses. |
| `tests/unit/rules/test_options.py` | PH-2841 ranking: nearer facility preferred, continue infeasible. |
| `tests/unit/rules/test_trigger.py` | Debounce, exact bounds, and above-upper behavior from config. |
| `tests/unit/sim/test_temperature.py` | Event shape and the three scenarios. |
| `tests/unit/incidents/test_lock.py` | One lock per shipment, including concurrent acquires. |
| `tests/unit/incidents/test_repository.py` | Create, legal and illegal transitions, stale updates, lock release. |
| `tests/unit/incidents/test_dynamo_repository.py` | Same incident rules on the DynamoDB adapter and fake client. |
| `tests/unit/incidents/test_debounce.py` | Streak reaches T1's threshold of 2, then a safe reading clears it. |
| `tests/unit/inbound/test_normalize.py` | WhatsApp text and media normalization. |
| `tests/unit/inbound/test_dedupe.py` | In-memory 7-day dedupe, judged by the clock. |
| `tests/unit/inbound/test_dynamo_dedupe.py` | DynamoDB dedupe, including a retry that does not write. |
| `tests/unit/support/fake_dynamo.py` | Fake low-level DynamoDB client used by the adapter tests. |
| `tests/unit/test_app.py` | CDK app fails clearly when no account is resolved. |
| `tests/unit/test_data_stack.py` | Data-stack resources and outputs. |

## Project config

| File | Role |
| --- | --- |
| `pyproject.toml` | Package metadata, Python 3.11+, pytest, and ruff. Runtime dependencies are pydantic, jsonschema, and the CDK libraries. |

## Run the tests

```text
python -m pytest
ruff check .
```
