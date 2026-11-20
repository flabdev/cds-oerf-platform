# Approach: Tenant catalog in DynamoDB (Settings UI)

**Status:** Proposed (not implemented)  
**Parent:** [desk-settings-approach.md](desk-settings-approach.md)  
**Related:** `sim/seed/`, `docs/interfaces.md` §6, ADR-009, ADR-013, `scripts/seed_runtime.py`, `oerf/platform/api/dashboard.py`  
**Not here:** Live WhatsApp SSM → [settings-live-contacts-approach.md](settings-live-contacts-approach.md). New tenant creation → [tenant-lifecycle-approach.md](tenant-lifecycle-approach.md).

## 1. Goal

Demo tenants (`t1`, `t2`, `sbx`) are authored as **`sim/seed/{tenant_id}/*.json`**, then copied into DynamoDB by `scripts.seed_runtime`. Runtime Lambdas read **Dynamo**, not files (with a few script/sandbox exceptions).

Goals:

1. **DynamoDB** is the operator **source of truth** for tenant reference data (contacts, facilities, shipments, fault codes, published config).
2. **Tenant admins** edit that data in the desk **Settings** tabs (Contacts, Facilities, etc.) without editing repo JSON.
3. **`sim/seed`** remains available as **CI fixtures** / optional import (§8).

## 2. Current state (`sim/seed`)

`sim/seed/load.py` loads all `*.json` under each tenant directory.

| File | Dynamo target | Runtime use |
|------|----------------|-------------|
| `tenant.json` | `META` | Timezone, brand, subscribed `domains[]` |
| `contacts.json` | `CONTACT#`; mocked `PHONE#` | Registry, engagements |
| `facilities.json` | `FACILITY#` | Diagnose, options |
| `shipments.json` | `SHIPMENT#`; routing `DEVICE#` | Ingest, demos |
| `domains/cold_chain_excursion.json` | `CONFIG#…` (after publish) | Incidents, thresholds |
| `cadence/*.json` | `CADENCE#` | Engagements |

`seed_runtime` maps `party_role` → **synthetic** 555-01xx for mocked channels; placeholder `${PHONE_*}` tokens in JSON are not stored as-is.

**Still reading files:** `seed_runtime`, `reset`, `demo`, `dev_preflight` (driver slot names), `devices.expected_demo_devices`, `sandbox/audience` (shipment pool), unit tests.

**Admin API today:** `GET/PUT /admin/tenants/{tid}/contacts|facilities|faultcodes`, domain config validate/publish, cadence — see `dashboard.py`. **Not wired in prod** `services_from_env()` (§10).

## 3. Architecture

```
Desk Settings (catalog tabs) → DashboardQueryFunction → Tenant table (+ routing for DEVICE# / mocked PHONE#)
```

- No browser AWS SDK (see parent doc).
- Edits write **row shapes** already defined in `docs/interfaces.md` pattern 5.

## 4. Data model (UI ↔ Dynamo)

| Entity | Key | Editable fields (indicative) |
|--------|-----|------------------------------|
| Contact | `CONTACT#{id}` | `party_role`, `label`, `email`, `opt_in`, `live_contacts`, `scripted`, `timezone` |
| Facility | `FACILITY#{id}` | `name`, `drive_min`, geo, capacity, `contact_id`, ranges |
| Shipment | `SHIPMENT#{id}` | `product_profile`, `driver`, `device`, `truck`, … |
| Fault code | `FAULTCODE#{code}` | Evidence catalog (not in `sim/seed` today) |
| Domain config | `CONFIG#…` | Draft/publish per domain (ADR-013) |
| Cadence | `CADENCE#…` | Ladder definitions |

**Mocked `e164` on `CONTACT#`:** optional; use one shared writer module with `seed_runtime` to avoid conflicting synthetic mapping.

**Real E.164 for live WhatsApp:** not on tenant rows — [settings-live-contacts-approach.md](settings-live-contacts-approach.md).

**`PHONE#` index:** create/update/delete contacts must call the same rules as `DynamoPhoneRegistry` (mocked numbers only from this feature).

## 5. API design

**Recommended:** resource-oriented REST (Option B in the earlier draft).

| Method | Path |
|--------|------|
| `GET` / `PUT` / `DELETE` | `/admin/tenants/{tid}/contacts/{contact_id}` |
| Same pattern | `/facilities/{id}`, `/shipments/{id}`, `/faultcodes/{code}` |
| Existing | `/domains/{domain}/config`, `/cadence` |

Alternative: implement **`DynamoCatalogStore`** for existing bulk `GET/PUT` catalog routes (faster, worse for concurrent edits).

Validation: JSON Schema or Pydantic for catalog rows; domain config keeps `Draft202012Validator`. Audit: operation + ids, not PII.

## 6. Settings UI (this doc only)

| Tab | Content |
|-----|---------|
| Contacts | Roles, labels, opt-in, **link to Live numbers** for `live_contacts` |
| Facilities | Drive times, capacity, contact link |
| Shipments & devices | Warn on ingest impact; restrict writes if needed |
| Fault codes | Code list for vision/evidence |
| Allowance / config | Existing publish flow; note ADR-013 snapshot text |
| Cadence | Later |

Implement under `web/src/components/settings/` (or similar); API helpers in `session.js`.

## 7. Coordination with live-contacts (no duplication)

| Concern | Owner doc |
|---------|-----------|
| Who exists (`contact_id`, `party_role`, `live_contacts`) | **This doc** |
| SSM path, SecureString, inbound `PHONE#` from SSM | **Live-contacts doc** |
| Slot list for GET live-contacts | **Dynamo contacts**, not `sim/seed` (after catalog P0) |

## 8. `sim/seed` after migration

| Strategy | Use |
|----------|-----|
| Fixture mirror | CI loads JSON → assert import matches Dynamo |
| Export script | `export_tenant_seed` from Dynamo for review |
| Operator import | `seed_runtime` becomes **import from files**, not daily workflow |

Detach `load_seed` from `dev_preflight`, `devices.expected_demo_devices`, and `audience` over time (§11 in old doc).

## 9. Config and operational risk

- **ADR-013:** publishing config does not change open incidents.
- **Shipments/devices:** changes affect ingest **immediately** — confirm in UI.
- **Contact rename vs new id:** open engagements may hold old `contact_id`.

## 10. Infrastructure

- Dashboard Lambda: tenant table R/W (already); routing table for `PHONE#` / `DEVICE#` when catalog edits contacts or devices.
- Wire `DynamoConfigRepository`, `ConfigPublisher`, catalog store in `services_from_env()`.
- **No SSM** in catalog Lambda unless you deliberately merge roles (prefer separate IAM statements per doc).

## 11. Phases

| Phase | Deliverable |
|-------|-------------|
| P0 | Wire services; GET lists from Dynamo |
| P1 | PUT/DELETE contacts & facilities; mocked `PHONE#`; desk tabs |
| P2 | Shipments, devices, fault codes; export/import |
| P3 | Scripts read Dynamo; `sim/seed` = fixtures only |
| P4 | Cadence editor; ADR-009 schema forms (stretch) |

## 12. Acceptance

1. Tenant admin edits contact/facility in desk; Dynamo updates without `seed_runtime`.
2. Mocked demo uses Dynamo registry.
3. Live rehearsal still uses live-contacts doc + `USE_MOCK_WHATSAPP=false`.
4. CI passes without AWS credentials.
5. No real numbers in git.

## 13. Open questions

1. Bulk catalog PUT vs item REST first?
2. Shipments/devices in MVP or scripts-only?
3. Hard delete vs `opt_in=false`?
4. Read-only Settings for `ops_user`?

---

*Planning only.*
