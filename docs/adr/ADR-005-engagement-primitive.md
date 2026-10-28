# ADR-005: Engagement primitive

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

`oerf-engage-party` is the reusable engagement workflow. Tenant config supplies the cadence policy: ISO-8601 durations, anchors, quiet hours, the final question, the exhaustion action, and stop conditions. A communication profile supplies tone and the phrases a message must not use.

[ADR-025](README.md) amends the policy key. The deployed map is `role.ask`, not a single policy per role. Cold-chain examples are `driver.diagnose`, `facility.accept`, and `ops_manager.approve`.

## Reasons

Reminder timing differs by party and by question. Hard-coded delays in each playbook step would fork the engine for every domain.

## Consequences

A new domain adds policies and templates. It does not add a second sender ([ADR-008](ADR-008-channel-router.md)). A reminder to someone who has not replied has to be an approved WhatsApp template. Quiet hours defer a rung. They do not skip it. A callback pause does not move the exhaustion deadline ([ADR-030](README.md)).
