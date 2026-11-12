# C15 — Reroute projection: what makes `Assess` choose a facility (and the seed values)

Date: 2026-11-12
From: Vivek (C15, C7, C9, C12, F1, I2)
To: Mudita (B1 — driver contacts, shipments and the three-driver roster)
Tracker: C15 "Reroute scenario: what makes Assess choose a facility"; depends on C2, B1.
Purpose: answer "what input does `Assess` use for the *continue* projection?", and give the
seed values for the second shipment. **One code change is required first** — see §4.

---

## 1. Answer — the input `Assess` uses for "*continue*"

`eta_min_at_excursion` is **never read by any code**. It is a dead seed field today.

- `oerf/platform/agent/handlers.py:381` builds the continue option with a hard-wired drive
  time: `RecoveryOption(id="continue", drive_min=0.0, kind="continue")`. The shipment (and its
  `eta_min_at_excursion`) is available in the context (`handlers.py:208`, copied from the
  ingest input at `oerf/platform/ingest/handler.py:363`), but `_recovery_options`
  (`handlers.py:364-382`) only reads `id`, `drive_min` and `capacity_available` from the
  facility rows and never touches the shipment.
- The only shipment-derived number that reaches the projection is `used_minutes`
  (`handlers.py:445`: `elapsed = _minutes_between(_ship_time(workflow, context), now)` →
  `AssessmentContext(used_minutes=elapsed, ...)` at `handlers.py:450-459`).
- `git grep eta_min_at_excursion` finds it only in the three seed files and three seed tests.
  Nothing under `oerf/` reads it.

So changing `PH-2841`'s `eta_min_at_excursion` from `46` to `120` could not change the ranking:
the value is inert. **That is the whole reason the reroute scenario never fires on distance.**

## 2. How the three options are projected (verified against the live code)

`rank_options` / `_project` — `oerf/platform/rules/options.py:92-117`:

```
upper            = product profile upper limit (8 °C for PP-2-8-45, t1 config)
used             = minutes above the upper limit so far (AssessmentContext.used_minutes)
t_arrive         = drive_min(option)
peak             = temperature_c + rise_rate × (t_arrive + unload_min)
cooldown_min     = max(0, peak − upper) / cooldown_rate
projected_above  = used + t_arrive + unload_min + cooldown_min     ← facility options
projected_above  = used + t_arrive + unload_min                    ← continue (no cooldown credit)
spare            = allowance − projected_above
feasible         = capacity_available and spare ≥ min_spare_allowance_min
rank             = feasible sorted by spare desc, then drive time, then id
```

Constants from `domains/cold_chain_excursion/defaults.json` (t1 inherits them):
`allowance = 45`, `unload = 4`, `rise_rate = 0.03`, `cooldown_rate = 0.12`,
`min_spare = 5`, `upper = 8`. t1 facilities (`sim/seed/t1/facilities.json`):
`FAC-A drive_min = 9`, `FAC-B drive_min = 21`.

Numbers below are the real `rank_options` output.

| Option | drive used | projected (used=0, T=8.6) | spare | feasible |
| --- | --- | --- | --- | --- |
| `continue` | **0 (hard-wired)** | 4.00 | 41.00 | yes |
| `FAC-A` | 9 | 21.25 | 23.75 | yes |
| `FAC-B` | 21 | 36.25 | 8.75 | yes |

`continue` is ranked first, so `oerf_status_update` ("continue to your destination") is sent.

## 3. Why no seed value can fix this on its own (proof)

With `drive_min(continue) = 0`, `continue.spare = allowance − used − 4` and, at T = 8.6 °C,
`FAC-A.spare = allowance − used − 21.25`. The continue option's spare is therefore **always
17.25 minutes larger** than FAC-A's, at every `used`:

| used | continue spare | FAC-A spare | FAC-A feasible? | recommendation |
| --- | --- | --- | --- | --- |
| 0 | 41.00 | 23.75 | yes | continue |
| 10 | 31.00 | 13.75 | yes | continue |
| 18.75 | 22.25 | 5.00 | yes | continue |
| 19 | 22.00 | 4.75 | **no** | continue |
| 30 | 11.00 | −6.25 | **no** | continue |
| 36 | 5.00 | −12.25 | **no** | continue |
| 37 | 4.00 | −13.25 | **no** | **escalate** |

So today `Assess` can only ever recommend **`continue`** (while anything is feasible) or
**`escalate`** (when nothing is). The *only* route to a facility recommendation is a driver
answering **`unit_stopped`**, which forces `continue_feasible=False`
(`handlers.py:447`, `handlers.py:464`). No shipment seed can change this, because the seed
value that would drive it is not read.

## 4. The change required (one line) — feed the remaining ETA into the continue option

The frozen rules-engine spec already defines this and the golden test encodes it, but
production does not implement it:

- `OERF_Spec.html`, page 16 (calibration): *"Continue: **remaining ETA = 46 − 3 = 43 min**;
  projected = used + remaining ETA + unload, with no cooldown term."*
- `tests/unit/rules/test_options.py:100` encodes exactly that: `RecoveryOption(id="DESTINATION",
  drive_min=43, kind="continue")` (i.e. `46 − 3`), and asserts DESTINATION is **infeasible**
  (`projected ≈ 50`) with `FACILITY_A` recommended.

**Requested change** (owner: the `Assess`/rules seam — C15/Vivek; needs a reviewer since it
touches a frozen-formula path): in `_recovery_options`
(`oerf/platform/agent/handlers.py:364-382`), set the continue option's `drive_min` to the
remaining journey instead of `0.0`:

```
eta = context["shipment"].get("eta_min_at_excursion")     # minutes to destination at excursion
used = <the same elapsed already computed in assess()>
continue drive_min = max(0.0, eta − used)                 # remaining ETA; omitted → 0 (unchanged)
```

Keep the current behaviour when the shipment has no `eta_min_at_excursion` (drive stays `0`),
so nothing regresses for tenants that do not seed one.

Under that wiring, at `used ≈ 0`, T = 8.6 °C, allowance 45, unload 4, min_spare 5:

- `continue` is **feasible** while `remaining_eta ≤ 36`.
- `continue` is **recommended** (outranks FAC-A) while `remaining_eta < 17.25`.
- `FAC-A` is recommended from `remaining_eta ≥ 17.25`; `continue` is infeasible from
  `remaining_eta ≥ 37`.

## 5. Proposed seed values (for B1, second shipment)

Two live-run facts set the numbers:

- `used_minutes` at `Assess` is the **wall-clock time from the opening reading to the driver's
  reply**, not zero: `Diagnose` sets `ship_time` to the trigger timestamp (`handlers.py:292-297`)
  and the driver engagement (`EngageDriverDiagnosis`) runs **before** `Assess`. So the driver
  should still answer promptly, but the ranking is robust to it (below).
- The **ranking** between continue and a facility is independent of `used` and of temperature at
  the trigger, because both projections add the same `used`. At T = 8.6 °C:
  `continue.proj = used + eta_rem + 4` and `FAC-A.proj = used + 21.25`, so
  `continue` outranks `FAC-A` exactly when `eta_rem < 17.25`, at any `used`. Only *feasibility*
  moves with `used`: `FAC-A` drops out once `used > 18.75`; the continue option (whose
  projection stops shrinking once `used ≥ eta`) drops out much later.

`PH-2841` and the new shipment share the same t1 allowance (45) and the same facilities, so the
**destination ETA is the only lever**. Given the tracker's instruction — *"PH-2841 stays the
'continue' story"* — the two ETA windows do not overlap:


### Primary proposal (keeps PH-2841 as the continue story)

| | PH-2841 (continue) | NEW PH-2842 (reroute) |
| --- | --- | --- |
| `shipment_id` | `PH-2841` (unchanged) | `PH-2842` |
| `device` | `RF-7781` (unchanged) | `RF-7783` |
| `driver` | `C-T1-DRV` (unchanged) | `C-T1-DRV-ANANDA` |
| `product_profile` | `PP-2-8-45` (unchanged) | `PP-2-8-45` |
| **`eta_min_at_excursion`** | **15** (was 46) | **60** |
| outcome | continue proj 19.00, spare 26.00 → **`continue` recommended**, `oerf_status_update` | continue proj 64.00, spare −19.00 → **infeasible**; FAC-A spare 23.75 → **`FAC-A` recommended**, `oerf_driver_reroute` |

`eta = 60` gives a clear "destination unreachable" margin; `eta = 46` (the spec's value) also
works (continue proj 50.00 → infeasible, FAC-A recommended). Pick 46 if you want to keep the
spec's number for the reroute shipment.

Also carry the usual fields on the new row so ingest and the desk read a complete shipment:
`truck` (e.g. `TRK-03`), `destination` (e.g. `Sample receiving depot 2`), `value_usd` (e.g.
`240000`), `storage_phase: false`. Use the same `PK`/`SK`/`tenant_id` shape as the existing row:

```json
{
  "tenant_id": "t1",
  "PK": "TENANT#t1",
  "SK": "SHIPMENT#PH-2842",
  "shipment_id": "PH-2842",
  "product_profile": "PP-2-8-45",
  "truck": "TRK-03",
  "device": "RF-7783",
  "driver": "C-T1-DRV-ANANDA",
  "destination": "Sample receiving depot 2",
  "eta_min_at_excursion": 60,
  "value_usd": 240000,
  "storage_phase": false
}
```

### Alternate (spec-frozen, no change to PH-2841)

If the team prefers not to touch PH-2841's seed: leave `PH-2841 = 46` (it then **is** the
reroute/spec-golden case — destination ~52 min, "too late", FAC-A preferred, matching
`tests/unit/rules/test_options.py`) and give the **new** shipment a reachable destination
(e.g. `eta_min_at_excursion = 15`) as the continue story. This matches the frozen spec's
PH-2841 narrative exactly but inverts which shipment tells which story, so it contradicts the
tracker's C15 wording. **This is a product decision — see §7.**

## 6. Ripple / things to update together

- Nothing else needs code: `seed_demo_devices` and `reset._reseed_devices` already glob
  `sim/seed/*/shipments.json` (`oerf/platform/channels/devices.py:80-102`,
  `scripts/reset.py:316-350`), so the new row is registered as a device automatically and
  ingest resolves `RF-7783 → (t1, PH-2842)` with no extra wiring. Drive it with
  `make dev-sensor PROFILE=… ACCOUNT=… TENANT=t1 SHIPMENT=PH-2842 DEVICE=RF-7783`.
- Seed assertions for the Primary proposal must move with the new values: `PH-2841` 46 → 15 and
  the new `PH-2842 = 60`:
  `tests/unit/seed/test_t1_seed.py:79`, `tests/unit/scripts/test_seed_runtime.py:74-76`,
  `tests/unit/seed/test_t2_sbx_seed.py:18`. (`tests/unit/rules/test_options.py` is unaffected —
  it constructs its own context — and `tests/unit/agent/test_ph2841_act.py` still passes, because
  at `eta = 15` continue stays recommended and the reroute test uses `unit_stopped`.)
- The desk/driver reroute message needs no change: `NotifyDivert` already sends
  `oerf_driver_reroute` (shipment, facility name, drive minutes, map link) when
  `action.kind == "facility"` (`domains/cold_chain_excursion/statemachine.asl.json`;
  `docs/interfaces.md` v1.36).

## 7. Decision: Option A

**Option A approved.** PH-2841 remains the continue story with ETA 15; PH-2842 is the distance-based reroute story with ETA 60. This is least disruptive to the existing walks: Scenario 1 continues on PH-2841, Scenario 2 can continue to use its driver-"Stopped" reroute, and the new distance-based reroute walk uses PH-2842.


## 8. Decision and implementation status

Decision: **Option A approved**. PH-2841 remains the continue story with ETA 15; PH-2842 is the distance-based reroute story with ETA 60.

Implemented in this change:
- `Assess` now uses `max(0, eta_min_at_excursion - used_minutes)` for continue's drive time; missing ETA retains the former zero-minute behavior.
- T1 seed now contains PH-2841 (ETA 15) and PH-2842 / RF-7783 / C-T1-DRV-ANANDA (ETA 60).
- Seed/device and assessment regression tests cover the new data and facility recommendation.

Still pending: run the dev sensor/driver walk on PH-2842 (continue infeasible → FAC-A recommended → driver reroute received) and record its result in `docs/validate-scenarios.md`. That scenario document is not present on this branch; it exists on `origin/develop` and should be updated when the dev validation is performed.
