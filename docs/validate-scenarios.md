# Validate the dev scenarios

Operator sheet for the three cold-chain walks on the **dev** stage (`us-east-1`). Commands run on a laptop. They do not print phone numbers, task tokens, or payloads. `PROFILE` is the AWS profile. `ACCOUNT` is that profile's 12-digit account id. Use Python 3.11 (`.venv/bin/python`).

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

Open the desk and leave it open so the timeline updates while the walk runs. Sign in twice, in two browser windows: once as the ops user (`ops-t1-dev@example.com`) to follow the incident, and once as the tenant admin (`admin-t1-dev@example.com`) for the QA step.

### Live or mocked router

Dev is currently deployed with live channels (manual run with `channels_live=true`; router `USE_MOCK_WHATSAPP=false`). On a live router:

- `make dev-reply` refuses to run, so the "without a phone" boxes below cannot be ticked.
- WhatsApp, SMS and email are all live. SMS reaches only numbers verified in the SNS SMS sandbox while the account is in it; email reaches only SES-verified recipients while SES is in its sandbox.

To run the no-phone walks, deploy once with `channels_live=false`, run them, then deploy again with `channels_live=true`. For the demo, the live walks are the ones that matter.

```text
gh workflow run deploy.yml --ref develop -f mode=deploy -f channels_live=false
gh workflow run deploy.yml --ref develop -f mode=deploy -f channels_live=true
```

A manual run uses its own `channels_live` input (default `false`), not the dev environment's `CHANNELS_LIVE`. Always pass it explicitly.

### Live numbers (SSM)

Live sends and inbound ownership read phone numbers from SSM, as **SecureString** parameters. On **dev** the prefix is `/oerf/dev/demo-numbers`, not `/oerf/demo-numbers` (main). The router reads `DEMO_NUMBERS_PREFIX` from the deployed Lambda.

Drivers are **one parameter per contact**: `{prefix}/{tenant}/driver/{contact_id}`. Every other role is **one parameter per role**: `{prefix}/{tenant}/{role}`.

| Who | Parameter | Needed for |
| --- | --- | --- |
| Driver Mudita (`C-T1-DRV`) | `/oerf/dev/demo-numbers/t1/driver/C-T1-DRV` | walks on `PH-2841` |
| Driver Ananda (`C-T1-DRV-ANANDA`) | `/oerf/dev/demo-numbers/t1/driver/C-T1-DRV-ANANDA` | walks on Ananda's shipment (below) |
| Driver Vivek (`C-T1-DRV-VIVEK`) | `/oerf/dev/demo-numbers/t1/driver/C-T1-DRV-VIVEK` | three-driver rehearsal only |
| Ops manager | `/oerf/dev/demo-numbers/t1/ops_manager` | ops notices (scenario 1) |
| QA | `/oerf/dev/demo-numbers/t1/<qa role>` | QA notices, if sent on WhatsApp |
| Facility | `/oerf/dev/demo-numbers/t1/facility` | facility notices (scenario 2) |
| Fleet supervisor | `/oerf/dev/demo-numbers/t1/<supervisor role>` | escalation rung (scenario 3) |

Fill in the exact QA and supervisor role names from `sim/seed/t1/contacts.json` before the rehearsal. Facility A and B are separate contacts but share the facility role parameter.

Put a number in once. The value is read without echo and is never committed. Use the type of the existing parameters (`SecureString`):

```text
read -s "NUM?E.164 number, e.g. +9198XXXXXXXX: "; echo
aws ssm put-parameter --profile <aws-profile> --region us-east-1 \
  --name /oerf/dev/demo-numbers/t1/driver/<contact-id> \
  --type SecureString --value "$NUM" --overwrite
unset NUM
```

Confirm a parameter exists without printing the value:

```text
aws ssm describe-parameters --profile <aws-profile> --region us-east-1 \
  --parameter-filters "Key=Name,Values=/oerf/dev/demo-numbers/t1/driver/<contact-id>" \
  --query "Parameters[].[Name,Type]" --output text
```

Then write the routing-table `PHONE#` ownership rows inbound checks against (reads SSM; does not print numbers). The contact must have `live_contacts: true` in the seed:

```text
STAGE=dev .venv/bin/python -m scripts.seed_runtime --stage dev \
  --profile <aws-profile> --tenant t1 --yes --live-contacts --roles driver
```

Expect a line like `applied t1 live-contacts driver 2`. A missing parameter prints `skipped t1 live-contacts driver/<contact-id> (no parameter)`.

`make dev-preflight` lists each required demo-number name and whether it exists. It does not print values. Every row in the table above that a walk needs must show as present.

### Which shipment rings which phone

| Driver | Contact | Shipment | Device |
| --- | --- | --- | --- |
| Mudita | `C-T1-DRV` | `PH-2841` | `RF-7781` |
| Ananda | `C-T1-DRV-ANANDA` | `PH-3102` | `RF-9102` |
| Vivek | `C-T1-DRV-VIVEK` | `<fill in from sim/seed/t1>` | `<fill in>` |

The commands below use `PH-2841` / `RF-7781`. For a walk on Ananda's or Vivek's phone, use their shipment and device instead. The three-driver rehearsal needs all three rows seeded and all three driver parameters present.

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

Requires a router with WhatsApp mock mode on (see "Live or mocked router").

```text
make dev-reply PROFILE=<aws-profile> ACCOUNT=<account-id> TENANT=t1 BUTTON=running
```

- [ ] Runs end to end without phones (`dev-sensor` / `dev-reply`)

### With a real WhatsApp phone

Do not run `dev-reply`. The driver's phone receives template `oerf_driver_checkin` for the shipment. Tap **Running**.

The child sends one session message, `Thanks, noted: Running. We're checking the shipment now.`, and ends `answered` / `unit_running`. Assess keeps continue. Act sends `oerf_status_update`: shipment id, `continue to your destination`, `we're monitoring the temperature`.

- [ ] Runs end to end with a real WhatsApp phone

### QA review

Verify reads the stored readings. The in-range series above is already long enough, so the first Verify can contain the excursion. The loop waits 60 seconds and tries again, up to 20 times, while the latest reading is still hot or the in-range run is shorter than 5 minutes.

After the report is generated, the run waits at `QAReviewWait` for a reviewer's verdict (up to 7 days). On the desk, a tenant admin or platform admin sees **QA review pending**, then **Approve** (release), **Reject — quarantine**, or **Reject**. An ops user sees the pending line and no verdict buttons. Re-open the incident if the buttons are not there yet: the callback row is not broadcast. The same verdict can still be given from the terminal:

1. In the browser window signed in as the tenant admin (`admin-t1-dev@example.com`), open the developer tools and copy the session storage value `oerf.idToken`.
2. Run:

```text
make demo-review-qa STAGE=dev TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id> \
  INCIDENT=<incident-id> DECISION=qa_pass
```

The command prompts for the token without echoing it (or reads `OERF_ID_TOKEN`). It refuses when no review is pending. The ops user cannot give a verdict; the admin's token is required.

Expect `queued QA review tenant=t1 incident=<incident-id> decision=qa_pass disposition=release`. The run resumes, records the release, and closes.

### Pass

On the desk, in order: the incident opens, the driver wait clears, the decision is continue, Verify moves to contained, a report exists, QA records a release, the incident is **CLOSED**. `demo-status` shows the parent path through `Assess`, `Act`, `NotifyContinue`, `VerifyLoop`, `RecordContainment`, `GenerateReport`, `QAReviewWait`, `QA`, and `Close`.

A run that ends in `RecordFailure` instead of `Close` does not pass, even though the execution itself reports SUCCEEDED (the failure path catches the error). Record the parent state and the first failed step.

## Scenario 2 — the driver stops, the workflow reroutes

The driver answers **Stopped**, so continuing is infeasible. Assess ranks the facilities, the workflow secures `FAC-A`, and Act sends the driver a reroute. This shows the workflow deciding from a real reply. Reset first, publish the shared sensor, and answer **Stopped**.

### Scripted

Same limit as scenario 1: `dev-reply` runs only while the router is mocked.

```text
make dev-reply PROFILE=<aws-profile> ACCOUNT=<account-id> TENANT=t1 BUTTON=stopped
```

- [ ] Scripted run works

### With a real WhatsApp phone

Tap **Stopped** on the check-in. The driver receives `oerf_driver_reroute` for `FAC-A`.

- [ ] Live run works

### Pass

`demo-status` shows `Assess`, `SecureFacility`, then `Act`, and the decision notice is `oerf_driver_reroute` for `FAC-A`. The rest of the walk (Verify, report, QA review, Close) follows scenario 1.

### Not demoable on this definition: facility decline and fallback

`SecureFacility` does not contact the facility or wait for an answer. With no partner reply on the workflow it records a pre-approved facility as accepted, so the first facility (`FAC-A`) is always accepted. `SecureFacilityFallback` (to `FAC-B`) runs only when the workflow already carries a decline and a next option, and nothing in a walk puts one there. The fallback is covered by the #66 unit tests; present it from the slides. Facility engagement (contact the facility and wait for accept or decline) is tracked as follow-up work after the demo.

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

On a live router:

- The SMS rung fails while the SNS account is in the SMS sandbox, unless the driver's number is verified there. Expect an error line on that rung; note it on the screenshot.
- The voice rung records the attempt but places no call. Note it on the screenshot.
- The supervisor escalation needs the fleet supervisor's parameter (see "Live numbers").

After exhaustion the parent leaves the driver engagement and runs Assess. A missing answer keeps continue.

- [ ] Earlier run with a silent driver captured (timeline and screenshots)

### Pass

The saved desk timeline shows the wait and the move on without a driver answer. The saved child history shows the check-in, the reminder, the supervisor escalation, and exhaustion at 4 minutes.
