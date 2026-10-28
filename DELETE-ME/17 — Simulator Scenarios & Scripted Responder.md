# 17 — Simulator Scenarios & Scripted Responder

<aside>
✅

**What this page fixes:** how the simulator produces telemetry for every test scenario, what the scripted responder replies, and which S-tests each scenario supports. Times are **scenario minutes**; real time is scenario time divided by the tenant clock multiplier. Values marked **Synthetic (new)** are test data chosen for this page.

</aside>

## Simulator contract

### Inputs

| Flag | Meaning | Default |
| --- | --- | --- |
| `--tenant` | `t1`, `t2` or `sbx` | required |
| `--shipment` or `--device` | Shipment ID (device resolved from seed) or device ID | tenant's seed shipment |
| `--scenario` | A name from the catalog below | `happy` |
| `--seed` | Random seed for noise | 2841 |
| `--speed` | Clock multiplier | tenant `clock_multiplier` (20 for demo tenants) |
| `--dry-run` | Print events instead of publishing | off |

### Output

A `TemperatureReading` event every **`PT30S`** of scenario time (**Synthetic (new)** reading interval), published with `PutEvents` to EventBridge: `source: sim.reefer`, `detail-type: TemperatureReading`, `detail {device_id, ts, temperature_c, lat, lon, seq}` (page 11). `seq` increases by 1 per reading. Coordinates move along a straight synthetic track near the page 11 example and stop at the chosen facility.

### Phases

| Phase | Curve | Ends when |
| --- | --- | --- |
| normal | 5.2°C plus noise | Scenario failure time |
| failing | Linear from 5.1°C to 8.1°C over 8 min (5.1, 5.8, 6.6, 7.4, 8.1 at 2-minute points) | First reading above 8°C |
| excursion | +0.03°C per min (the page 04 `rise_rate`) | Storage-phase flag is true |
| storage | Piecewise curve to the first reading back in range (scenario specific) | Back in range |
| cooldown | Down to 5.0°C over 10 min, then normal | End of run |
- **Storage-phase flag:** the workflow's `SetStoragePhase` step (page 11, step 9) sets `storage_phase: true` on the shipment item (`TENANT#{tid}` / `SHIPMENT#{sid}`). The simulator reads it with `GetItem` before each reading. The item and attribute name are a proposal for the Architect to confirm, because page 11 does not name the item.
- **Determinism:** noise is Gaussian with σ 0.02°C from a fixed seed, clamped to ± 0.05°C, and never applied to a scenario's key points. The same seed and scenario always produce the same readings.

## Scenario catalog

All curves start in `normal` at 10:06 and fail at 10:08 unless stated. Path states follow FR-I3.

| Scenario | Purpose | Temperature curve | Party behaviour | Expected path | S-tests |
| --- | --- | --- | --- | --- | --- |
| `happy` | Main story PH-2841 | 8.1 at 10:16; 8.6 at 10:33 (storage); 8.3 at 10:36, 7.9 at 10:40, 7.4 at 10:43, 6.8 at 10:45 | All reply promptly; A accepts; QA releases at 11:05 | OPEN → DIAGNOSING → ASSESSING → AWAITING_APPROVAL → SECURING_FACILITY → IN_TRANSIT → VERIFYING → CONTAINED (10:45) → AWAITING_DISPOSITION → CLOSED (11:05) | S1, S9, S12, S13, S20, S25, S26, S27, S35 |
| `facility_a_declines` | Fallback to B | As `happy` to 10:22; arrival at B 10:44; storage from 9.0 at 10:46, falling 0.12°C/min; back in range about 10:54; contained about 11:00 | A declines; B accepts (T1). T2 at 30 min: no feasible option | T1: as `happy` via B. T2: SECURING_FACILITY → ESCALATED (B re-checked at the decline: page 16, G11/G12) | S2, S3 |
| `keeps_rising` | Reopen after transfer | As `happy` to 10:33; then +0.02°C/min (8.6 → 8.9 by 10:48) | As `happy` | VERIFYING → REOPENED, facility and QA alerted | S7 |
| `debounce_blip` | No incident on one reading | Normal; one reading of 8.2 at 10:10; back to 5.3 | None | No incident | S16 |
| `concurrent_duplicate` | One incident only | As `happy`, but the first two readings above 8°C are published twice, in parallel | As `happy` | One incident, one execution | S17 |
| `driver_silent` | Driver follow-up path | As `happy` | Driver silent. Variants: `--reply-after voice` (replies on WhatsApp after the voice rung); `--late-reply` (taps the first message after rung 3) | Follow-ups to supervisor escalation; continues with "cause unknown" | S4, S8, S19 |
| `approver_silent_within` | Auto-proceed | As `happy` | Ops manager silent | Auto-proceed at `PT2M` (T1) or `PT1M` (T2), approver notified | S5 |
| `approver_silent_outside` | Escalate approval | As `happy` | Ops manager silent; Facility A fee overridden to 650 USD | AWAITING_APPROVAL → escalation to `ops_director` | S6 |
| `a_no_capacity` | Skip A | As `facility_a_declines`, about one minute earlier (B requested first): arrival at B about 10:43, storage from about 9.0°C at about 10:45. | A `capacity_available: false`; B accepts | As `happy` via B, no request to A | Golden case 3 |
| `none_feasible` | No option | As `happy`, no storage phase | T2 at 30 min; A has no capacity | ASSESSING → ESCALATED, `qa_prewarn`; ops picks an option on the dashboard | S3, S43, golden case 4 |
| `transfer_unconfirmed` | Facility silent after arrival | As `happy` to 10:31; excursion continues until the dashboard action sets storage | Facility never confirms; ops taps Confirm transfer (twice in the S50 variant) | IN_TRANSIT → ESCALATED → VERIFYING → CONTAINED | S31, S42, S50 |
| `reroute_unacked` | Driver silent after reroute | As `happy` (truck still arrives) | Driver never taps On my way; facility confirms | Stays IN_TRANSIT with supervisor escalation, then VERIFYING; at close, pending follow-ups stop | S15, S32 |
| `rise_during_qa` | Reopen while awaiting QA | As `happy` to 10:45; rises to 8.2 at 10:50; back to 7.6 at 10:58 | QA replies to the old message after the reopen, then to the new report | AWAITING_DISPOSITION → REOPENED → VERIFYING → CONTAINED → CLOSED | S34, S45 |
| `false_alarm_cancel` | Probe fault, cancel | Normal; readings 8.4 and 8.5 at 10:10–10:10:30; normal again | Driver reports E40 and a 5°C display; ops cancels | OPEN/DIAGNOSING → CANCELLED | S29 |
| `voice_note` | Voice note without Transcribe | As `happy` | Driver first sends a voice note, then a button | As `happy` | S18 |
| `photo_evidence` | Display photo lanes | As `happy` | Driver sends a photo from the page 18 set. Variants: `clear`, `low_confidence`, `unreadable` | As `happy` after confirmation or typed code | S10, S37 |
| `call_me` | Human callback | As `happy` | Variant `driver`: Call me at check-in. Variant `approver`: OTHER, then Call me | Callback request to the callback role; deadlines unchanged | S23, S33, S44 |
| `opt_out` | Keyword opt-out | As `happy` | Facility A sends STOP mid-request; separately a free-text "please don't message me" | Opt-out stops that contact; the free text does not | S22, S36 |
| `sandbox_dashboard` | Judge, no phone | As `happy` on a new `SBX-S{session}` shipment | Scripted responder plays every party | As `happy` in `sbx` | S38, S48 |
| `sandbox_join` | Judge phone mode | As `happy` on `SBX-J{last4}` | Responder sends JOIN from new numbers, a repeat JOIN and a JOIN from a T1 number | Contact and shipment created, repeat returns the same shipment, T1 number ignored, cap reached on the 21st | S39, S46, S47 |

## Scripted responder

- **Delivery path:** the responder never calls `SendTaskSuccess`. It publishes each reply to the same SNS topic as real inbound messages, in the same event shape, with a provider message ID `sim-{uuid}` and the `context.id` of the outbound message it answers. The reply then flows SNS → SQS → inbound handler, so the page 11 rule (only `inbound_handler` resumes workflows) still holds. Email replies use the SES inbound event shape.
- **Trigger:** a responder Lambda reads outbound MSG timeline events from the incident table stream, keeps only contacts with `scripted: true`, and creates a one-time EventBridge Scheduler schedule for the reply delay. The schedule targets the responder Lambda, never a CDS API (ADR-008). This is test infrastructure only (**Synthetic (new)**).
- **When it is on:** T2 and `sbx` contacts are always `scripted: true` (except a judge who joined by phone). T1 contacts are `scripted: false`; the integration suite loads the seed profile `t1-scripted`, which sets them to true for automated runs only.
- **Button payloads:** the IDs below (`BTN_…`) are **Synthetic (new)** and must match the WhatsApp template buttons (open question Q10).
- **Dependency:** outbound messages to scripted contacts go to placeholder numbers. The channel router needs a rule to record them without calling the provider. This is not yet specified on page 11.

### Replies in `happy`

| Party role | Replies to | Reply | Intent | Delay |
| --- | --- | --- | --- | --- |
| `driver` | `driver_checkin` (10:17) | `BTN_RUNNING`, then text "E17 compressor fault" | answer (fault code E17) | +0.5, +1 min |
| `ops_manager` | `ops_approval` (10:20) | `BTN_APPROVE` | answer | +0.5 min |
| `facility` (A) | `facility_request` (10:21) | `BTN_ACCEPT` | answer | +1 min |
| `driver` | `driver_reroute` (10:22) | `BTN_ON_MY_WAY` | answer | +0.3 min |
| `facility` (A) | `transfer_confirm_request` (10:31) | `BTN_TRANSFERRED` | answer | +2 min (10:33) |
| `qa` | `qa_report_ready` (10:45) | `BTN_RELEASE` (WhatsApp) or "RELEASE" (email, S12) | answer | +20 min (11:05) |

### Changes from `happy` in other scenarios

| Scenario | Party role | Replies to | Reply | Intent | Delay |
| --- | --- | --- | --- | --- | --- |
| `facility_a_declines` | `facility` (A), then (B) | `facility_request` | A: `BTN_DECLINE`; B: `BTN_ACCEPT` | decline, answer | +0.5, +1 min |
| `driver_silent` | `driver` | `driver_checkin`, `driver_reminder`, `driver_call` | None; variants reply `BTN_STOPPED` after the voice rung or tap the first message late | answer | after rung 4 or rung 3 |
| `approver_silent_within`, `approver_silent_outside` | `ops_manager` | `ops_approval`, `approval_reminder` | None | — | — |
| `transfer_unconfirmed` | `facility` (A) | `transfer_confirm_request`, `facility_reminder` | None; ops user taps Confirm transfer (dashboard action) at 10:40 | — | — |
| `reroute_unacked` | `driver` | `driver_reroute` | None | — | — |
| `rise_during_qa` | `qa` | first `qa_report_ready`, then the updated report | `BTN_RELEASE` to the first at 10:52 (gets `reopened_late_reply`); `BTN_RELEASE` to the updated report | answer | +7 min; +5 min after the new report |
| `false_alarm_cancel` | `driver` | `driver_checkin` | Text "display shows E40 and 5 degrees, unit running" | answer (fault code E40) | +0.5 min; ops cancels at +2 min |
| `voice_note` | `driver` | `driver_checkin`, then `voice_note_unsupported` | Audio file, then `BTN_RUNNING` and text "E17" | answer | +0.5, +1 min |
| `photo_evidence` | `driver` | `driver_checkin`, then `evidence_confirm` or `evidence_unreadable` | Photo from page 18; then `BTN_YES` or text "E17" | answer | +0.5, +1 min |
| `call_me` | `driver` or `ops_manager` | `driver_checkin` or `ops_approval` | `BTN_CALL_ME`; approver variant first sends `BTN_OTHER` | call-me | +0.5 min |
| `opt_out` | `facility` (A) | `facility_request` | Text "STOP"; second run: "please don't message me" | keyword opt-out; second run: unrelated | +0.5 min |
| `sandbox_join` | new numbers | — | Text "JOIN" from `+9100000003xx` numbers; one repeat; one from a T1 placeholder | — | 1 min apart |

## S-test mapping

| S-tests | Scenario |
| --- | --- |
| S1, S9 (responder sends the same reply twice), S12 (QA replies by email), S13 (publish config mid-run), S20, S25 (close attempt by API after the run), S26, S35 (T2 after publish) | `happy` |
| S27 | `happy` with seed variant: shipment without ETA |
| S2, S3 | `facility_a_declines` (T1, T2) |
| S4, S8, S19 | `driver_silent` and its variants |
| S5 / S6 | `approver_silent_within` / `approver_silent_outside` |
| S7 | `keeps_rising` |
| S10, S37 | `photo_evidence` |
| S15, S32 | `reroute_unacked` |
| S16 / S17 / S18 | `debounce_blip` / `concurrent_duplicate` / `voice_note` |
| S21 | `happy` with the responder variant `concern` (driver text "why do I have to go to another warehouse?") |
| S22, S36 | `opt_out` |
| S23, S33, S44 | `call_me` |
| S24 | `happy` with the approval sent as an RCS card (only if RCS inbound is confirmed) |
| S29 | `false_alarm_cancel` |
| S31, S42, S50 | `transfer_unconfirmed` |
| S34, S45 | `rise_during_qa` |
| S43 | `none_feasible` |
| S38, S48 / S39, S46, S47 | `sandbox_dashboard` / `sandbox_join` |
| Tenant isolation and latency tests | `happy` on T1 and T2 at the same time |
| S11 | n/a: channel fault injection in the router, no telemetry needed |
| S14 | n/a: calendar unit test of `compute_next_rung` |
| S28, S30, S51 | n/a: admin API, validation and permission tests |
| S40, S49 | n/a: retention and expiry checks on stored items |
| S41 | n/a: template catalog audit |

## Timing

- Every wait scales with the tenant clock multiplier: reading interval, follow-up rungs, `exhaust_after`, `stable_in_range`, `poll_interval`, `callback.wait` and responder delays. Real time = scenario time ÷ multiplier. The multiplier is above 1 only for tenants with `demo: true` (page 03).
- **`happy` at ×20:** 10:06 to 11:05 is 59 scenario minutes, about **2 min 57 s** of real time. One reading every 1.5 s.
- **Real people at ×20:** the T1 approval wait (`PT2M`) lasts 6 real seconds and a driver follow-up (`PT1M`) 3 seconds, too short for someone tapping a demo phone. Until open question Q9 is decided, rehearsals with real phones should expect auto-proceed and escalation to fire early.