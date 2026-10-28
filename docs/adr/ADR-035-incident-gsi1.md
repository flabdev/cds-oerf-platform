# ADR-035: Incident GSI1 lists by status

**Status:** Accepted
**Date:** 2026-10-18
**Decider:** Architect

## Context

`GET /incidents` lists incidents for the caller's tenant. The incident table key is `pk` / `sk`, which is one incident. A list by state needs an index. The desk's live list does not use this index. It uses the stream ([ADR-010](ADR-010-live-updates.md)).

## Decision

`IncidentTable` has one global secondary index, `GSI1`, projection `ALL`.

- Partition key `GSI1PK` = `TENANT#{tenant_id}#STATUS#{state}`
- Sort key `GSI1SK` = `opened_at` (UTC `YYYY-MM-DDTHH:MM:SSZ`)

The repository writes both attributes on incident `META`. The item field remains `state`. The table stays on-demand, keeps its stream, and is not replaced to add the index. There is no second GSI. The open-incident lock replaced that lookup ([ADR-016](README.md)).

## Consequences

Items written before the attributes existed stay out of the index until they are written again. `GET /incidents` queries `GSI1` once per state. This is pattern 12 in [the interface contract](../interfaces.md).
