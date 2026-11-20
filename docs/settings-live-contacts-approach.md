# Approach: Live demo numbers (Settings UI)

**Status:** Proposed (not implemented)  
**Parent:** [desk-settings-approach.md](desk-settings-approach.md)  
**Related:** `docs/validate-scenarios.md`, `oerf/platform/channels/address.py`, `scripts/seed_runtime.py` (`seed_live_contacts`)  
**Not here:** Contact CRUD / roles → [settings-tenant-catalog-approach.md](settings-tenant-catalog-approach.md). New tenants → [tenant-lifecycle-approach.md](tenant-lifecycle-approach.md).

## 1. Goal

Replace operator shell + `aws ssm put-parameter` and optional `python -m scripts.seed_runtime --live-contacts` with desk **Settings → Live numbers**. **Tenant admins** assign E.164 to party slots for **live** WhatsApp rehearsal (`USE_MOCK_WHATSAPP=false` on compute).

## 2. AWS SDK scope

| Layer | SDK |
|-------|-----|
| Python platform / dashboard Lambda | `boto3` (`pyproject.toml`) |
| Web desk | None — REST + Cognito only |

Do not call SSM from the browser.

## 3. Behaviour today

### 3.1 SSM names (`address.py`)

| Party | Parameter |
|-------|-----------|
| Non-driver roles | `{prefix}/{tenant_id}/{party_role}` |
| Drivers | `{prefix}/{tenant_id}/driver/{contact_id}` |

Prefix: `/oerf/dev/demo-numbers` (dev) or `/oerf/demo-numbers` (main). Router uses `DEMO_NUMBERS_PREFIX`.

### 3.2 Inbound requires routing sync

SSM is used for **outbound** live address resolution. **Inbound** needs matching `PHONE#` on the routing table (`seed_live_contacts`). UI should **PutParameter + sync one row** in one operation (preferred).

### 3.3 Out of scope here

- `USE_MOCK_WHATSAPP` (deploy flag)
- `dev_preflight` (can share status badges later)
- Mocked 555-01xx on `CONTACT#` (catalog doc)

## 4. Architecture

```
Desk Settings → Live numbers tab → DashboardQueryFunction → SSM SecureString + Routing PHONE#
```

Slot list **source:** tenant table **contacts** with `live_contacts !== false`, not `sim/seed` (after catalog P0). Same rules as `live_driver_parameter_names` / `seed_live_contacts` paths.

## 5. API (proposed `interfaces.md` bump)

| Method | Path | Behaviour |
|--------|------|-----------|
| `GET` | `/admin/tenants/{tid}/live-contacts` | Slots: `contact_id`, `party_role`, `label`, `parameter_name`, `status` (`missing` \| `configured` \| `invalid`); **no** full E.164 |
| `PUT` | `/admin/tenants/{tid}/live-contacts/{contact_id}` | Body `{ "e164": "+…" }`; validate; SSM overwrite; sync `PHONE#` |
| `POST` | `…/live-contacts/sync` (optional) | Bulk sync |
| `DELETE` (optional) | same as PUT path | Clear parameter / policy TBD |

Auth: `tenant_admin` or `platform_admin` via `_admin_tenant`. Optional masked hint: `mask_number()` (`oerf/platform/sandbox/audience.py`).

## 6. IAM

Dashboard Lambda needs (today it has **none** of this):

- `DEMO_NUMBERS_PREFIX` env (match compute stack)
- `ROUTING_TABLE_NAME` + write for ownership items
- `ssm:GetParameter`, `ssm:PutParameter` on `…/parameter/oerf/{stage}/demo-numbers/{tenant}/*` only
- Row in `docs/security/iam-exceptions.md`

## 7. Settings UI

- Tab under Settings (see parent IA); not a top-level duplicate of Contacts.
- Table: label, role, status, **Set number** modal (E.164 helper from `validate-scenarios.md`).
- Success: “Saved” + “Routing updated” if sync included.
- Roles: `tenant_admin` / `platform_admin`; hide write from `ops_user` unless product says otherwise.

## 8. Security

- No E.164 in logs or audit payload (ids + operation only).
- Tenant from JWT only.
- Tests: synthetic `+15550100…` only.

## 9. Dependencies

| Prerequisite | Reason |
|--------------|--------|
| Catalog contacts in Dynamo | Defines slots and `live_contacts` |
| `services_from_env()` SSM + routing ports | Persist in prod |

## 10. Phases

| Phase | Deliverable |
|-------|-------------|
| P0 | Backend GET/PUT, IAM, tests |
| P1 | Desk Live numbers tab |
| P2 | Bulk sync, preflight-style badges |

## 11. Acceptance

1. Admin sets `ops_manager` and driver slot from UI (dev).
2. `dev_preflight` PASS on parameter **names** (values not printed).
3. Live walk: outbound + inbound ownership per `validate-scenarios.md`.

---

*Planning only.*
