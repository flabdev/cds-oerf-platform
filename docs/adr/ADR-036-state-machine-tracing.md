# ADR-036: Trace the playbook state machines

**Status:** Accepted
**Date:** 2026-10-19
**Decider:** Architect

## Context

The cold-chain state machine was created without X-Ray tracing. The workflow stub in the same stack already traced. Operators need state transitions in the same trace as the functions.

## Decision

`oerf-cold-chain` and `oerf-engage-party` both run with X-Ray tracing enabled. Each machine has its own log group, retained for 30 days, at `ERROR`, with execution data omitted. The state machine definitions are unchanged. Enabling tracing does not replace either machine.

## Consequences

X-Ray shows the state transitions. Execution data stays out of the logs because a payload can carry a message. The request that opened this decision named only the cold-chain machine. The engagement machine is traced the same way.
