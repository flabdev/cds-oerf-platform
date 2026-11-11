# Validate the dev scenarios

Operator sheet for the three cold-chain walks on the **dev** stage (`us-east-1`). Commands run on a laptop. They do not print phone numbers, task tokens, or payloads. `PROFILE` is the AWS profile. `ACCOUNT` is that profile's 12-digit account id. Use Python 3.14 (`.venv/bin/python`).

The rehearsal commands are in [run-the-demo.md](run-the-demo.md). This page is the pass criteria for the three stories.

Reset between walks. A second sensor run needs a new EventBridge id. Replaying the same reading id is ignored for 7 days.

```text
make dev-reset PROFILE=<aws-profile> TENANT=t1
```

## Before the first walk

```text
make dev-preflight PROFILE=<aws-profile>
make dev-check     PROFILE=<aws-profile> TENANT=t1
```

`dev-check` must show device `RF-7781` on shipment `PH-2841`. If it does not:

```text
make dev-seed PROFILE=<aws-profile> TENANT=t1
```

Open the desk and leave it open so the timeline updates while the walk runs.

The manual deploy with **channels_live** on is a live router. `make dev-reply` refuses a live router. The no-phone boxes below need a later deploy with channels mocked.

### Live driver number (SSM)

Live WhatsApp sends and inbound ownership use one **SecureString per contact**, not one number per role. On **dev** the prefix is `/oerf/dev/demo-numbers`, not `/oerf/demo-numbers` (main).

Shipment `PH-2841` uses driver contact `C-T1-DRV`. The parameter name is:

```text
/oerf/dev/demo-numbers/t1/driver/C-T1-DRV
```

Do not use `/oerf/demo-numbers/t1/driver` on dev. The router reads `DEMO_NUMBERS_PREFIX` from the deployed Lambda (`/oerf/dev/demo-numbers` on dev) and resolves drivers as `{prefix}/{tenant}/driver/{contact_id}`.

Put the real E.164 number once. Use your profile and region; the value is not committed to the repo.

```text
aws ssm put-parameter --profile <aws-profile> --region us-east-1 \
  --name /oerf/dev/demo-numbers/t1/driver/C-T1-DRV \
  --type SecureString \
  --value '<E.164, e.g. +919849873580>' \
  --overwrite
```

Confirm the parameter exists without printing the value:

```text
aws ssm get-parameter --profile <aws-profile> --region us-east-1 \
  --name /oerf/dev/demo-numbers/t1/driver/C-T1-DRV \
  --query Parameter.Name --output text
```

Then write the routing-table `PHONE#` ownership row inbound checks against (reads SSM; does not print numbers):

```text
STAGE=dev .venv/bin/python -m scripts.seed_runtime --stage dev \
  --profile <aws-profile> --tenant t1 --yes --live-contacts --roles driver
```

Expect a line like `applied t1 live-contacts driver 1`. A missing parameter prints `skipped t1 live-contacts driver/C-T1-DRV (no parameter)`.

`make dev-preflight` lists each required demo-number name and whether it exists. It does not print values.

Only **drivers** are contact-scoped. Ops, QA, and facility sends on live channels use `{prefix}/{tenant}/{role}`, for example `/oerf/dev/demo-numbers/t1/facility` and `/oerf/dev/demo-numbers/t1/ops_manager`. Facility A and B in seed are separate contacts but share the facility role parameter unless you extend addressing in code.

## Shared sensor

Two readings above 8 °C open the incident. One reading opens nothing. `SCENARIO=excursion` ends on a single reading above 8 °C, so it does not open `PH-2841`. Publish temperatures instead. Event times are 30 seconds apart and end when the command starts. Twelve readings at 5.2 °C cover the 5-minute in-range window Verify uses (`stable_in_range` is 5 minutes, range 2–8 °C).

```text
STAGE=dev .venv/bin/python -m scripts.demo sensor \
  --stage dev --profile <aws-profile> --account <account-id> \
  --tenant t1 --shipment PH-2841 --device RF-7781 \
  --temps 8.1,8.6,5.2,5.2,5.2,5.2,5.2,5.2,5.2,5.2,5.2,5.2,5.2,5.2
```

The command waits up to 30 seconds for a new `t1-INC-*` execution on `oerf-dev-cold-chain`. On the desk the incident is diagnosing, and the options area says **Waiting for the driver's answer**.

Check progress without opening payloads:

```text
make demo-status TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id> STAGE=dev
```

## Scenario 1 — the main story

A reading for `PH-2841` goes above 8 °C, the incident opens on the desk, the driver answers the check-in, Assess and Act choose an action, the driver gets the decision notice, the temperature is already back in range, Verify contains it, a report is generated, QA reviews, and the incident is **CLOSED**. The desk timeline moves through the whole walk.

Publish the shared sensor, then answer **Running**.

### Without a phone

Requires a router with WhatsApp mock mode on. On the current live deploy this command stops.

```text
make dev-reply PROFILE=<aws-profile> ACCOUNT=<account-id> TENANT=t1 BUTTON=running
```

- [ ] Runs end to end without phones (`dev-sensor` / `dev-reply`)

### With a real WhatsApp phone

Do not run `dev-reply`. The phone receives template `oerf_driver_checkin` for `PH-2841`. Tap **Running**.

The child sends one session message, `Thanks, noted: Running. We're checking the shipment now.`, and ends `answered` / `unit_running`. Assess keeps continue. Act sends `oerf_status_update`: shipment id, `continue to your destination`, `we're monitoring the temperature`.

- [ ] Runs end to end with a real WhatsApp phone

### Pass

On the desk, in order: the incident opens, the driver wait clears, the decision is continue, Verify moves to contained, a report exists, QA records a release, the incident is **CLOSED**. `demo-status` shows the parent path through `Assess`, `Act`, `NotifyContinue`, `VerifyLoop`, `RecordContainment`, `GenerateReport`, `QA`, and `Close`.

Verify reads the stored readings. The in-range series above is already long enough, so the first Verify can contain the excursion. The loop waits 60 seconds and tries again, up to 20 times, while the latest reading is still hot or the in-range run is shorter than 5 minutes.

QA on this machine does not wait for a WhatsApp **Release** or a desk action. The step needs a review already on the workflow (`approved` true and disposition `release`, `quarantine`, or `reject`). Nothing in the walk writes that review. A run that reaches `QA` without it fails the step and goes to `RecordFailure` instead of `Close`. Record that parent state. **CLOSED** is not met until a review is on the workflow before `QA`.

## Scenario 2 — a facility declines

`FAC-A` declines, the workflow selects `FAC-B` on its own, and `FAC-B` accepts. Reset first, publish the shared sensor, and answer **Stopped** so continue is infeasible and Assess ranks `FAC-A`, then `FAC-B`.

### Scripted

Same limit as scenario 1: `dev-reply` runs only while the router is mocked.

```text
make dev-reply PROFILE=<aws-profile> ACCOUNT=<account-id> TENANT=t1 BUTTON=stopped
```

`dev-reply` posts the driver check-in button only (`Running` or `Stopped`). It does not post facility **Accept** or **Decline**.

- [ ] Scripted run works

### Pass

`demo-status` shows `SecureFacility`, then `SecureFacilityFallback`, then `Act`, and the decision notice is `oerf_driver_reroute` for `FAC-B`.

On this definition that path does not run from a reply. `SecureFacility` does not start a facility engagement and does not wait. With no partner reply on the workflow it records a pre-approved facility as accepted, so the first facility (`FAC-A`) is accepted and Act sends the reroute for `FAC-A`. The fallback runs only when the workflow already carries a decline and a next option. Record the parent path. The box is met only when `SecureFacilityFallback` selects `FAC-B`.

## Scenario 3 — a silent driver

Show the cadence ladder and the escalation from a saved timeline. Do not wait this out during a demo.

Capture it once, on a spare incident. Publish the shared sensor and do not answer the check-in. The driver policy `critical_field` exhausts after 4 minutes. Screenshot the desk timeline and the `oerf-dev-engage-party` execution history for that incident. Later demos use those pictures.

The rungs, from the start of the engagement:

| After | Channel | Message |
| --- | --- | --- |
| 0s | WhatsApp | `oerf_driver_checkin` |
| 90s | SMS | driver reminder |
| 1 min | WhatsApp | `oerf_driver_reminder` |
| 2 min | voice | `driver_call` (the router does not place a call) |
| 3 min | WhatsApp | `oerf_supervisor_escalation` to the fleet supervisor |
| 4 min | | exhausted, `continue_without`, cause `cause_unknown` |

After exhaustion the parent leaves the driver engagement and runs Assess. A missing answer keeps continue.

- [ ] Earlier run with a silent driver captured (timeline and screenshots)

### Pass

The saved desk timeline shows the wait and the move on without a driver answer. The saved child history shows the check-in, the reminder, the supervisor escalation, and exhaustion at 4 minutes.
