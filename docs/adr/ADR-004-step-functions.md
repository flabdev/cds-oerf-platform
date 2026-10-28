# ADR-004: Step Functions Standard and task tokens

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

AWS Step Functions Standard orchestrates each playbook. `oerf-cold-chain` is the cold-chain machine. `oerf-engage-party` is the nested engagement machine, started with `startExecution.sync`.

A rung that needs a reply uses `lambda:invoke.waitForTaskToken`. The channel router stores the token and sends the message. Only the inbound handler calls `SendTaskSuccess`. Timeouts drive the cadence.

The cold-chain path that is deployed is GatherContext, Diagnose, EngageDriver, Assess, Act, a decision notice, then Verify. Assess runs after the driver engagement returns. An early sketch put Assess before that engagement. The state machine is the authority.

## Reasons

An incident waits on people, branches, and can run longer than an Express workflow allows. A hand-rolled state engine would hide the path that the execution history shows. Scheduler alone does not give that history or the task-token pause.

## Consequences

Executions can last up to a year. The execution name is derived from the tenant and the incident id, and inbound rebuilds that name for cancel ([ADR-019](ADR-019-manual-cancellation.md)).

These cold-chain tasks invoke their own functions: GatherContext, Diagnose, Assess, Act, VerifyLoop, QA, Close, RecordFailure, Escalate, and Cancel. NotifyContinue and NotifyDivert invoke the channel router. OPEN, Approval, SecureFacility, HandleReopen, GenerateReport, and the containment bookkeeping states still invoke the shared stub. On the engagement machine, cadence, interpret, and the router sends are real. Callback handling, ladder reset, the final question, and the exhaustion action still invoke the stub.

`oerf-report-generator` is deployed. The GenerateReport state does not call it yet.
