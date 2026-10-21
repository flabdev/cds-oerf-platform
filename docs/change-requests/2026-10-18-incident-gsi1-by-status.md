# Change request: incident table GSI1 by status

**Priority:** normal. Blocks `GET /incidents` (OERF-302).
**To:** data-stack owner
**From:** OERF-304
**Date:** 2026-10-18
**Contract:** [docs/interfaces.md](../interfaces.md) section 6, pattern 12
**File you own:** `infra/stacks/data_stack.py` (`IncidentTable`)

## What to add

One global secondary index on `IncidentTable`. The table today is `pk` / `sk`, on-demand, streams `NEW_AND_OLD_IMAGES`, no GSI. [tests/unit/test_data_stack.py](../../tests/unit/test_data_stack.py) asserts that GSIs are absent. That assertion has to change in your change, not in OERF-304.

| Property | Value |
| --- | --- |
| Index name | `GSI1` |
| Partition key | `GSI1PK` (string) |
| Sort key | `GSI1SK` (string) |
| Projection | `ALL` |

Access pattern: list incidents for one tenant in one state.

- `GSI1PK = TENANT#{tid}#STATUS#{state}`
- `GSI1SK = opened_at` (UTC `YYYY-MM-DDTHH:MM:SSZ`, so lexicographic order is time order)

`state` is the incident field from the OERF-112 repository (`OPEN`, `DIAGNOSING`, and the rest of the state set in `docs/interfaces.md` section 4.2). The LLD writes the word `STATUS` into the key. The attribute on the item remains `state`.

OERF-112 writes `GSI1PK` and `GSI1SK` on incident META. This request only adds the index. Items written before those attributes exist stay out of the index until backfilled.

Adding a GSI is an online table update. It should not replace `IncidentTable`. Please confirm that in `cdk diff`. The table stays `RemovalPolicy.RETAIN`, on-demand, with the existing stream. Do not add the other GSIs the LLD dropped (the lock item replaced GSI2).

## Who is waiting

OERF-302 `GET /incidents` returns one row per incident for the caller's tenant. That query is pattern 12. Dashboard tenant-list subscriptions do not use this index; they use the incident stream.

## Acceptance

- `cdk diff` adds `GSI1` and does not replace the table.
- The data-stack unit test allows this index and still fails if a second GSI appears.
- No change to key attributes `pk` and `sk`.
