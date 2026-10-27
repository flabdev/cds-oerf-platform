# 16 — Seed Data & Calibration

<aside>
✅

**What this page fixes:** the exact seed records for T1, T2 and the judge sandbox (`sbx`), and the worked calibration of the rules engine. Seed files live in the repo under `sim/seed/{tid}/` and are loaded through CDK. Values marked **Synthetic (new)** are test data chosen for this page; everything else comes from pages 01–15 or the concept specification.

</aside>

## Purpose and scope

- **Seeded:** every record a tenant needs before its first incident (tenant table), the device and phone-registry items (routing table), and the domain template and platform settings (platform table).
- **Not seeded:** incidents, timelines, engagements, evidence and routing correlation items. These are created at runtime and removed by the reset (page 18).
- **Files:** `sim/seed/platform.json`, `sim/seed/t1/`, `sim/seed/t2/` and `sim/seed/sbx/`, one JSON file per record type (for example `contacts.json`, `facilities.json`).
- **Loader:** `python -m sim.seed load --tenant t1|t2|sbx|all`. CDK runs it after deployment as a custom resource; the reset runs it again. All writes are idempotent puts.
- **Owner:** Developer (page 12).

## Platform records

| Record | Key (PK / SK) | Content |
| --- | --- | --- |
| Domain template | `DOMAIN#cold_chain_excursion` / `TEMPLATE#v1` | JSON Schema, defaults, logical message catalog (page 11), state machine ARN |
| Platform settings | `SETTINGS` / `PLATFORM` | `sandbox.max_joins_per_day: 20` (ADR-032) |

## Seed inventory: T1

| Record type | Key (PK / SK) | Count | Values |
| --- | --- | --- | --- |
| Tenant META | `TENANT#t1` / `META` | 1 | Brand "Acme Pharma Logistics", `Asia/Kolkata`, `demo: true`, `clock_multiplier: 20`, domains `[cold_chain_excursion]` |
| Domain config | `CONFIG#cold_chain_excursion#v1` and `CONFIG#cold_chain_excursion#ACTIVE` | 2 | Page 04 sample with `unload_min: 4` (calibration below). Product profiles `PP-2-8-45` (2–8°C, 45 min) and `PP-15-25-120` (15–25°C, 120 min) |
| Cadence policies | `CADENCE#{policy}#v1` | 7 | `critical_field`, `driver_ack`, `approval_fast` (exhausts `PT2M`), `partner_request`, `transfer_confirm`, `review_normal`, `notify_only` (page 05) |
| Communication profile | `COMMS#cp_default_v1#v1` | 1 | As on page 05 |
| Calendars | `CALENDAR#cal_24x7`, `CALENDAR#cal_business` | 2 | `cal_24x7` for critical policies. `cal_business` **Synthetic (new)**: Mon–Sat 09:00–18:00 `Asia/Kolkata`, used by the QA-lead step of `review_normal` |
| Template map | `TEMPLATEMAP#{logical}` | 1 per catalog message | Every logical message on page 11 → WhatsApp template name and language, SMS text, voice script, email subject |
| Facilities | `FACILITY#FAC-A`, `FACILITY#FAC-B` | 2 | A: 6.8 km, 9 min. B: 14 km, 21 min. Both approved, capacity available. Emergency fee A 250 USD, B 300 USD (**Synthetic (new)**, see open question Q11) |
| Contacts | `CONTACT#{cid}` | 8 | One per party role, with two facility contacts (A and B). All `opt_in: true`. Phones and emails are placeholders (below) |
| Assets | `ASSET#TRK-01`, `ASSET#RF-7781` | 2 | Truck and reefer unit (generic, no vendor) |
| Shipments | `SHIPMENT#PH-2841` | 1 | Profile `PP-2-8-45`, destination 46 min away at the excursion, value 180,000 USD (**Synthetic (new)**, for FR-UI1) |
| Fault codes | `FAULTCODE#{code}` | 6 | Catalog below |
| Device | routing `DEVICE#RF-7781` / `META` | 1 | → `t1`, asset `RF-7781`, shipment `PH-2841` |
| Phone registry | routing `PHONE#{e164}` / `TENANT#t1#CONTACT#{cid}` | 8 | One per contact phone (ADR-029) |

## Seed inventory: T2 (differences from T1)

T2 is a copy of T1 except for these records:

| Record | T2 value |
| --- | --- |
| Tenant META | `TENANT#t2`, brand "Sample Vaccine 3PL" (**Synthetic (new)**), otherwise as T1 |
| Domain config v1 | Same as T1, including the **45-min** allowance at seed. The demo edits it live to 30 min (page 14) |
| `approval_fast` | Exhausts at `PT1M` (T1: `PT2M`) |
| Contacts | Own 8 contacts with their own placeholders. All `scripted: true`, because T2's parties are played by the scripted responder |
| Facilities | Own `FAC-A` and `FAC-B` with the same distances, drive times and fees as T1, so the calibration is identical |
| Asset, device, shipment | `RF-7782`, `TRK-02`, shipment `VX-3101` (**Synthetic (new)**), same profile and ETA as PH-2841 |
| Phone registry | 8 items under `TENANT#t2#CONTACT#…` |

## Seed inventory: `sbx`

| Record | `sbx` value |
| --- | --- |
| Tenant META | `TENANT#sbx`, brand "OERF Sandbox" (**Synthetic (new)**), `demo: true`, `clock_multiplier: 20` |
| Config, policies, profile, calendars, template map, fault codes | Copies of T1 (`approval_fast` at `PT2M`) |
| Facilities | `FAC-A`, `FAC-B` as T1 |
| Contacts | 8 seeded contacts, all `scripted: true`. In phone mode, JOIN adds the judge as the driver contact (FR-J2) |
| Seed shipment and device | `SBX-0001` on device `RF-SBX-0001`. Dashboard-only sessions create `SBX-S{session}` at runtime; JOIN creates `SBX-J{last4}`. Runtime items carry a 24-hour `ttl` and are not seeded |

## Example records

One item per record type, using the key formats from pages 04 and 11.

```json
{"PK":"TENANT#t1","SK":"META","brand_name":"Acme Pharma Logistics","timezone":"Asia/Kolkata","demo":true,"clock_multiplier":20,"domains":["cold_chain_excursion"]}
{"PK":"TENANT#t1","SK":"CONFIG#cold_chain_excursion#ACTIVE","version":1}
{"PK":"TENANT#t1","SK":"CONFIG#cold_chain_excursion#v1","status":"published","product_profiles":[{"id":"PP-2-8-45","range_c":[2,8],"excursion_allowance_min":45,"allowance_mode":"cumulative"},{"id":"PP-15-25-120","range_c":[15,25],"excursion_allowance_min":120,"allowance_mode":"cumulative"}],"assessment":{"unload_min":4,"rise_rate_c_per_min":0.03,"cooldown_rate_c_per_min":0.12,"min_spare_allowance_min":5},"...":"rest as page 04"}
{"PK":"TENANT#t1","SK":"CADENCE#approval_fast#v1","urgency":"critical","exhaust_after":"PT2M","exhaust_action":{"type":"auto_proceed_if_within_limits"},"...":"rungs as page 05"}
{"PK":"TENANT#t1","SK":"COMMS#cp_default_v1#v1","...":"as page 05"}
{"PK":"TENANT#t1","SK":"CALENDAR#cal_business","timezone":"Asia/Kolkata","business_hours":{"mon_sat":["09:00","18:00"]},"holidays":[]}
{"PK":"TENANT#t1","SK":"TEMPLATEMAP#driver_checkin","whatsapp":{"template":"oerf_driver_checkin","language":"en"},"sms":"Temperature alert {shipment_id}. Pull over safely and check WhatsApp.","voice":"driver_call_v1","email":null}
{"PK":"TENANT#t1","SK":"FACILITY#FAC-A","name":"Facility A (sample cold store)","approved":true,"capacity_available":true,"range_c":[2,8],"distance_km":6.8,"drive_min":9,"emergency_fee_usd":250,"lat":17.47,"lon":78.42,"contact_id":"C-T1-FACA"}
{"PK":"TENANT#t1","SK":"CONTACT#C-T1-DRV","party_role":"driver","label":"Driver (T1)","whatsapp":"${PHONE_T1_DRIVER}","sms":"${PHONE_T1_DRIVER}","email":null,"opt_in":true,"scripted":false,"timezone":"Asia/Kolkata"}
{"PK":"TENANT#t1","SK":"ASSET#RF-7781","type":"reefer_unit","truck":"TRK-01","set_point_c":5}
{"PK":"TENANT#t1","SK":"SHIPMENT#PH-2841","product_profile":"PP-2-8-45","truck":"TRK-01","device":"RF-7781","driver":"C-T1-DRV","destination":"Sample receiving depot","eta_min_at_excursion":46,"value_usd":180000,"storage_phase":false}
{"PK":"TENANT#t1","SK":"FAULTCODE#E17","meaning":"Compressor fault","fixable_on_road":false,"action":"divert_to_cold_storage","synthetic":true}
{"PK":"DEVICE#RF-7781","SK":"META","tenant_id":"t1","asset":"RF-7781","shipment":"PH-2841"}
{"PK":"PHONE#+910000000101","SK":"TENANT#t1#CONTACT#C-T1-DRV"}
```

## Fault-code catalog (synthetic, not vendor codes)

The same six codes are seeded for every tenant.

| Code | Meaning | Fixable on the road | Action implication |
| --- | --- | --- | --- |
| E17 | Compressor fault (unit has power but does not cool) | No | Divert to cold storage; used by `happy` |
| E05 | Door-open sensor | Yes | Driver closes and checks the door; monitor for recovery |
| E09 | Defrost cycle running | Yes | Short rise is expected; monitor, no diversion unless it persists |
| E22 | Condenser fan fault | No | Divert to cold storage |
| E31 | Low fuel or power supply fault | Yes | Driver restores power; monitor |
| E40 | Temperature probe fault | No | Readings may be false; ask for a display photo; ops may cancel as a false alarm (S29) |

## Phone numbers and addresses

- **Notion and the repo hold placeholders only.** Phones use the fake range `+9100000001xx` (T1), `+9100000002xx` (T2) and `+9100000003xx` (`sbx`); emails use `example.com` (for example `qa@example.com`); people are role labels such as "Driver (T1)".
- **Real demo numbers** for the registered demo phones live in a git-ignored local file `sim/seed/.demo-numbers.env` and in AWS SSM Parameter Store as SecureString parameters `/oerf/demo-numbers/{tid}/{role}`. DevOps owns them (page 12).
- **Substitution:** contact files use tokens such as `${PHONE_T1_DRIVER}`. The loader resolves each token from SSM (or the local file when run locally). If no value exists, it writes the placeholder. It writes the matching `PHONE#` registry item with whichever value it used.
- Facility coordinates are synthetic points near the page 11 example (Hyderabad area). No real facility names or addresses.

## Calibration

### Inputs

- Scenario PH-2841 (page 01, concept specification): excursion reading 8.1°C at 10:16; assessment at 10:19; destination 46 min away at 10:16; Facility A 9 min, Facility B 21 min; allowance 45 min (T1, T2 at seed) or 30 min (T2 after the demo edit).
- Parameters (page 04): `rise_rate_c_per_min` 0.03, `cooldown_rate_c_per_min` 0.12, **`unload_min` 4** (reverted from 5 so that Facility B stays feasible when re-checked after a facility decline, golden case G11), `min_spare_allowance_min` 5, upper limit 8°C.

### Assumptions

1. `used` at assessment = 3 min (10:16 → 10:19).
2. `T_now` = 8.1 + 0.03 × 3 = **8.19°C**.
3. Unload counts as time above the limit (it is in the page 11 formula).
4. Continue: remaining ETA = 46 − 3 = 43 min; projected = used + remaining ETA + unload, with no cooldown term.
5. The spec values (A ≈ 24, B ≈ 36, continue ≈ 52) are rounded targets. Golden tests assert the computed values below exactly (± 0.1) and the spec targets within **± 3.5 min**.
6. When a facility declines, the remaining options are re-projected at the decline time. The reference decline is at 10:22 (used = 6 min, `T_now` = 8.28°C).

### Worked calculation

```
A:  T_peak = 8.19 + 0.03 × (9 + 4)  = 8.58   cooldown = 0.58 / 0.12 = 4.83
    projected = 3 + 9 + 4 + 4.83   = 20.8   (target 24, within ± 3.5)
B:  T_peak = 8.19 + 0.03 × (21 + 4) = 8.94   cooldown = 0.94 / 0.12 = 7.83
    projected = 3 + 21 + 4 + 7.83  = 35.8   (target 36, within ± 3.5)
Continue: projected = 3 + 43 + 4   = 50.0   (target 52, within ± 3.5)
B re-checked at 10:22: T_peak = 8.28 + 0.03 × (21 + 4) = 9.03   cooldown = 1.03 / 0.12 = 8.58
    projected = 6 + 21 + 4 + 8.58  = 39.6   spare = 45 − 39.6 = 5.4 (≥ 5, feasible)
```

A (20.8 min) sits 3.2 min below the rounded spec target of 24, inside the ± 3.5 min tolerance. A's peak of 8.58°C is consistent with the scenario's maximum of 8.6°C. With `unload_min` 5, B re-checked after the decline would be infeasible (spare 4.2 < 5).

### Golden-case table

| Case | Inputs | T_peak / cooldown | Projected (min) | Spare (min) | Feasible | Supports |
| --- | --- | --- | --- | --- | --- | --- |
| G1 T1, continue | 45 min, ETA 43 | — | 50.0 | −5.0 | No | Golden case 1, S1 |
| G2 T1, Facility A | 45 min, drive 9 | 8.58 / 4.83 | 20.8 | 24.2 | Yes (preferred) | Golden case 1, S1 |
| G3 T1, Facility B | 45 min, drive 21 | 8.94 / 7.83 | 35.8 | 9.2 | Yes (backup) | Golden case 1, S2 |
| G4 T2 at seed | 45 min | As G1–G3 | 50.0 / 20.8 / 35.8 | −5.0 / 24.2 / 9.2 | No / Yes / Yes | Golden case 2 |
| G5 T2 after edit, Facility A | 30 min, drive 9 | 8.58 / 4.83 | 20.8 | 9.2 | Yes | S35 |
| G6 T2 after edit, Facility B | 30 min, drive 21 | 8.94 / 7.83 | 35.8 | −5.8 | No | Golden case 8, S3 |
| G7 A has no capacity | T1, 45 min, A `capacity_available: false` | B as G3 | 35.8 | 9.2 | A excluded; B recommended | Golden case 3 |
| G8 None feasible | T2, 30 min, A unavailable (declined or no capacity) | B as G6 | 35.8 / 50.0 | −5.8 / −20.0 | No option | Golden case 4, S3, S43 |
| G9 Approval limits | A fee 250 USD ≤ 500, approved facility | — | — | — | Auto-proceed allowed | Golden case 5, S5 |
| G10 Approval outside limits | A fee overridden to 650 USD &gt; 500 | — | — | — | Escalate to `ops_director` | Golden case 5, S6 |
| G11 T1, B re-checked after A declines (10:22) | 45 min, used 6, `T_now` 8.28, drive 21 | 9.03 / 8.58 | 39.6 | 5.4 | Yes | S2, page 17 `facility_a_declines` |
| G12 T2 after edit, B re-checked after A declines (10:22) | 30 min, used 6, drive 21 | 9.03 / 8.58 | 39.6 | −9.6 | No → ESCALATED | S3 |

The observed scenario (10:16 to about 10:39 above 8°C, about 24 min) includes the 3-minute reroute lead (10:19 → 10:22) that the formula does not model. That difference is inside the tolerance. G11 holds only if A declines by about 10:22:20; after that B's spare falls below 5. The scripted responder declines at 10:21:30.

## Reconciliation with the early files

The early `seed_data.json` and `simulator.py` predate both the multi-tenant design and the build window. List them in the pre-existing code disclosure (page 14).

- [ ]  Split `seed_data.json` into `sim/seed/t1/` files with the key formats above; move playbook settings into the domain config v1.
- [ ]  Add `sim/seed/t2/`, `sim/seed/sbx/` and `sim/seed/platform.json`.
- [ ]  Replace demo phone numbers in the seed with tokens; move real values to SSM and `.demo-numbers.env`.
- [ ]  Keep `unload_min` at 4 and `min_spare_allowance_min` at 5, and add the facility fee, shipment value and `scripted` flags.
- [ ]  Extend `simulator.py` to the contract and scenario catalog on page 17, keeping the existing scenario names and the `--speed` and `--dry-run` flags.
- [ ]  Add unit tests for the golden cases G1–G12.