# Run the demo

Operator commands for one cold-chain rehearsal. They run on a laptop, in `us-east-1`. They are not Lambdas and they are not part of CI.

`PROFILE` is the AWS shared-config profile. `ACCOUNT` is the 12-digit account id that profile must be using. `TENANT` is `t1`, `t2`, or `sbx`. The commands refuse a caller in any other account before they read or write anything else. They do not print phone numbers, task tokens, ARNs, account ids, or raw payloads.

```text
make demo-preflight TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id>
make demo-prep      TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id>
make demo-sensor    TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id>
make demo-cleanup   TENANT=t1 PROFILE=<aws-profile> ACCOUNT=<account-id>
```

`PROFILE`, `TENANT`, and `ACCOUNT` are required. A missing one stops Make before Python starts.

For tenant `t1` the shipment and device default to `PH-2841` and `RF-7781`. Pass nothing else unless the rehearsal is for another seed row.

Deploy is a separate step. See [runbook-deploy.md](runbook-deploy.md).

## demo-preflight

Checks the account and stops on the first wrong caller. A matching account then prints one `PASS` or `FAIL` line for each of these:

- Stacks `OerfDataStack`, `OerfComputeStack`, `OerfApiStack`, and `OerfObservabilityStack` are in a `*_COMPLETE` status.
- State machines `oerf-cold-chain` and `oerf-engage-party` exist.
- A runtime-seed dry run would change nothing.

Exit `0` only when every line is `PASS`.

## demo-prep

Resets that tenant, reseeds it, and writes the runtime rows. The following reset dry run must report zero. The command prints one summary line.

The reset stops that tenant's `RUNNING` executions of `oerf-engage-party` and then `oerf-cold-chain`. It deletes that tenant's incidents, shipment locks, debounce rows, engagement routing rows, and one-way dedupe rows. Evidence under `tenants/{tenant}/` is deleted. Reports under `reports/{tenant}/` stay. The incident-id counter stays, so the next id continues. Tenant config and `INGEST#` dedupe rows stay.

## demo-sensor

Publishes two `TemperatureReading` events on the bus `oerf-events`, source `sim.reefer`: 8.1 °C, then 8.6 °C. Event time is thirty seconds apart. Wall time is four seconds apart. Two readings above 8 °C open the incident. One reading opens nothing.

The command then waits up to 30 seconds for a new `{tenant}-INC-*` execution on `oerf-cold-chain`. Each run uses a new EventBridge id. Replaying the same id is ignored for 7 days, and prep does not clear those `INGEST#` rows.

## demo-cleanup

Resets the tenant again and requires the dry run to report zero. Run this when the rehearsal is over. `make reset-test` and `make reset-demo` are the lower-level reset; they do not take `ACCOUNT`.
