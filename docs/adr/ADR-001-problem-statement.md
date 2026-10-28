# ADR-001: Problem statement, cold-chain first

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

Build the Operational Exception Resolution Framework. The first playbook is a cold-chain temperature excursion, built end to end. Other domains are configuration on the same platform, shown as a second tenant.

## Reasons

The platform has to show a long-running operational exception, several parties, and a decision a person can audit. A document pipeline would not exercise orchestration, messaging, or the desk. Starting with one playbook keeps the engine from growing ahead of a working path.

## Consequences

The product name is OERF. A second domain reuses the engagement workflow, the channel router, and the desk, and adds its own state machine and config.
