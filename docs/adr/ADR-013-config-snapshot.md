# ADR-013: Config snapshot per incident

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

Resolve tenant config when the incident opens. Store `config_snapshot` and `config_version` on the incident. A later publish applies only to incidents opened after it.

## Reasons

A decision has to be reproducible. The audit has to show which thresholds and which facility list were in force, even if an operator publishes a new allowance while the incident is open.

## Consequences

The desk allowance control publishes a new version and leaves open incidents on the snapshot they started with. Product profiles used for the band and the allowance come from that snapshot ([ADR-026](README.md)).
