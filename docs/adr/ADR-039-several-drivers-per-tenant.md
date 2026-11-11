# ADR-039: Several drivers per tenant, and one driver on two shipments

- **Status:** Accepted
- **Date:** 2026-11-11
- **Tracker:** B1, B2, B3, B5, B6, G4

## Context

The MVP addressed every driver of a tenant through one role-scoped number (`/oerf/demo-numbers/{tenant}/driver`). With several drivers, check-ins must reach the right person, and replies must land on the right shipment. A driver can also carry two shipments at once, so one phone can have two open engagements.

## Decision

1. **Drivers are addressed per contact.** A shipment names its driver contact; the router resolves `{prefix}/{tenant}/driver/{contact_id}` from SSM (#61). Other roles stay role-scoped (`{prefix}/{tenant}/{role}`). The prefix comes from the stage (ADR-037).
2. **Inbound ownership is per contact.** Seeding writes one `PHONE#` ownership row per live driver contact; inbound checks a reply against the engagement it belongs to, so one driver cannot answer for another (#61, #62).
3. **Matching a reply to a shipment:**
   - A button reply or a reply that quotes a message is matched by the **quoted message id**. That identifies the engagement, so it is always unambiguous.
   - **Unquoted free text** from a driver with **one** open engagement goes to that engagement.
   - Unquoted free text from a driver with **two or more** open engagements is **not routed**. The driver gets one clarification message asking them to reply to the specific check-in (or tap its button); the text is recorded on neither engagement.
4. **Tenant isolation holds:** a driver of tenant t2 replying cannot reach a t1 engagement, whatever the text.

## Consequences

- Several drivers per tenant work in parallel without cross-talk (covered by the three-driver isolation tests, #62).
- A driver on two shipments must use buttons or reply to the message; free text needs one extra step. This is preferred to guessing.
- Audience drivers who join with `JOIN DRIVER` follow the same rules (ADR-040).

## Alternatives considered

- **Use the latest engagement for unquoted free text:** simpler, but a wrong-shipment answer on a cold-chain excursion is worse than one extra message; rejected.
- **One driver may only carry one active shipment:** contradicts real fleet operations; rejected.
- **Free-text intent extraction to pick the shipment:** possible later with the agent (advisory only, ADR-007); out of scope now.
