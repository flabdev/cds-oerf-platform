# Run the demo

Operator commands for one cold-chain rehearsal. They run on a laptop, in `us-east-1`. They are not Lambdas and they are not part of CI.

`PROFILE` is the AWS shared-config profile. `ACCOUNT` is the 12-digit account id that profile must be using. `TENANT` is `t1`, `t2`, or `sbx`. `STAGE` is `main` or `dev`. There is no default. `main` also needs `ALLOW_MAIN=1`; without it the command stops before it calls AWS. The commands refuse a caller in any other account before they read or write anything else. They do not print phone numbers, task tokens, ARNs, account ids, or raw payloads.

```text
make demo-preflight TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id> STAGE=main ALLOW_MAIN=1
make demo-prep      TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id> STAGE=main ALLOW_MAIN=1
make demo-sensor    TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id> STAGE=main ALLOW_MAIN=1
make demo-cleanup   TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id> STAGE=main ALLOW_MAIN=1
```

`PROFILE`, `TENANT`, `ACCOUNT`, and `STAGE` are required. A missing one stops Make before Python starts. `STAGE=main` without `ALLOW_MAIN=1` stops the same way.

## Dev stage

Dev targets set `STAGE=dev`. They publish and read the dev names (`oerf-dev-events`, `OerfDataStack-dev`, `/oerf/dev/...`). They do not take `ALLOW_MAIN`.

```text
make dev-preflight PROFILE=<aws-profile>
make dev-seed      PROFILE=<aws-profile> TENANT=t1
make dev-users     PROFILE=<aws-profile> TENANT=t1 ROLE=ops_user EMAIL=ops@example.com
make dev-sensor    PROFILE=<aws-profile> ACCOUNT=<account-id> TENANT=t1 SHIPMENT=PH-2841 DEVICE=RF-7781 SCENARIO=excursion
make dev-reply     PROFILE=<aws-profile> ACCOUNT=<account-id> TENANT=t1
make dev-reset     PROFILE=<aws-profile> TENANT=t1
make dev-check     PROFILE=<aws-profile> TENANT=t1
make dev-diff      PROFILE=<aws-profile> ACCOUNT=<account-id>
make dev-deploy    PROFILE=<aws-profile> ACCOUNT=<account-id>
```

`dev-seed` runs `seed_runtime --yes` and then `verify_seed`. `TENANT` is `t1`, `t2`, or `sbx`. `dev-users` reads the password from the terminal; it is not an argument and it is not a file. `dev-sensor` publishes the scenario on `oerf-dev-events`. `dev-reset` applies a reseeded reset. `dev-check` is `verify_seed`. `dev-diff` and `dev-deploy` build `web/` and run CDK on the four `*-dev` stacks with the same context the dev workflow uses. Normal dev deploys happen by merging to `develop`. The laptop targets are for a rehearsal from this machine.

`dev-deploy` reads the phone-number id from `/oerf/dev/whatsapp/phone-number-id`, the shared inbound topic from main's `OerfComputeStack` (read-only), and `ALARM_EMAIL` from the environment or `gh variable get ALARM_EMAIL`. It stops before CDK if any of those is empty. `CHANNELS_LIVE` defaults to false.

### Live rehearsal

Before a live dev run:

- Put one SSM parameter per driver contact that should be live: `/oerf/dev/demo-numbers/{tenant}/driver/{contact_id}`. Other roles use `/oerf/dev/demo-numbers/{tenant}/{role}`. `make dev-preflight` prints the names and whether each one exists. It does not print the values.
- Set `live_contacts` true on those contacts in the seed. Contacts left false are skipped.
- Create the dev desk user with `make dev-users` (an `example.com` address, password typed at the prompt).
- Set `CHANNELS_LIVE=true` on the GitHub dev environment before the next develop deploy, or pass `CHANNELS_LIVE=true` to `make dev-deploy`.
- Confirm the account can call the Bedrock model the dev runtime uses. A missing model access fails the first live interpret, not the deploy.

For tenant `t1` the shipment and device default to `PH-2841` and `RF-7781`. Pass nothing else unless the rehearsal is for another seed row.

Deploy is a separate step. See [runbook-deploy.md](runbook-deploy.md).

## demo-preflight

Checks the account and stops on the first wrong caller. A matching account then prints one `PASS` or `FAIL` line for each of these:

- The stage's four stacks are in a `*_COMPLETE` status. On main those are `OerfDataStack`, `OerfComputeStack`, `OerfApiStack`, and `OerfObservabilityStack`. On dev each name ends in `-dev`.
- State machines `oerf-cold-chain` and `oerf-engage-party` exist on main. Dev uses `oerf-dev-cold-chain` and `oerf-dev-engage-party`.
- A runtime-seed dry run would change nothing.

Exit `0` only when every line is `PASS`.

## demo-prep

Resets that tenant, reseeds it, and writes the runtime rows. The following reset dry run must report zero. The command prints one summary line.

The reset stops that tenant's `RUNNING` executions of `oerf-engage-party` and then `oerf-cold-chain`. It deletes that tenant's incidents, shipment locks, debounce rows, engagement routing rows, and one-way dedupe rows. Evidence under `tenants/{tenant}/` is deleted. Reports under `reports/{tenant}/` stay. The incident-id counter stays, so the next id continues. Tenant config and `INGEST#` dedupe rows stay.

## demo-sensor

Publishes two `TemperatureReading` events on the stage bus (`oerf-events` on main, `oerf-dev-events` on dev), source `sim.reefer`: 8.1 °C, then 8.6 °C. Event time is thirty seconds apart. Wall time is four seconds apart. Two readings above 8 °C open the incident. One reading opens nothing.

The command then waits up to 30 seconds for a new `{tenant}-INC-*` execution on `oerf-cold-chain`. Each run uses a new EventBridge id. Replaying the same id is ignored for 7 days, and prep does not clear those `INGEST#` rows.

## demo-cleanup

Resets the tenant again and requires the dry run to report zero. Run this when the rehearsal is over. `make reset-test` and `make reset-demo` are the lower-level reset; they do not take `ACCOUNT`.
