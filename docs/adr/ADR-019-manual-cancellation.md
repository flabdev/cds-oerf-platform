# ADR-019: Cancel an incident with StopExecution

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

An operator in `platform_admin`, `tenant_admin`, or `ops_user` can cancel an incident from the desk. The action is `cancel` on `POST /incidents/{id}/actions`, with a client `action_id`. The API queues it to the inbound queue. The tenant comes from the token.

The inbound handler calls `states:StopExecution` on the cold-chain execution that ingest started. The execution name is rebuilt from the stored tenant and incident id. The handler then sets the incident to `CANCELLED` through the repository, which writes the timeline event and deletes the shipment lock. It does not call `SendTaskSuccess`.

A party cannot cancel an incident by message.

## Reasons

A false alarm has to stop the engagement and release the shipment so a later excursion can open a new incident. Stopping the execution from the desk API would add a second component that can end a workflow. Inbound already owns resume ([ADR-028](README.md)).

## Consequences

A duplicate `action_id`, or an incident already `CLOSED` or `CANCELLED`, does not call `StopExecution` again. `ExecutionDoesNotExist` means the execution has already finished and is not an error.

The state machine also has a `Cancel` task for a `cancel_requested` flag on the execution input. Ingest does not set that flag. Operator cancel is `StopExecution`, not that task.

The inbound role may stop executions of `oerf-cold-chain` only.
