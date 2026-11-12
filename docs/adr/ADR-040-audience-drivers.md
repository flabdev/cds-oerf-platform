# ADR-040: Audience driver contacts expire

**Status:** Accepted
**Date:** 2026-11-10
**Decider:** Mudita

## Context

Show & Tell needs any number of drivers on t1, not one SSM parameter per phone. The three team contacts stay seeded. An audience phone must not remain in the registry after the session, and the desk must not show the number. ADR-029 keeps a permanent row for each registered address. This decision amends that for audience joins only.

## Decision

On dev, `JOIN DRIVER` registers the sender as a t1 driver for 24 hours. The contact id is `AUD-` plus a hash, the display name is `Audience` plus the last four characters of that id, and the row carries consent time and an expiry. One seeded shipment is assigned from the tenant pool. The daily cap is 20. At expiry, reset, or an explicit remove, the contact, the phone index, and the audience row are deleted. The three seeded team phones are not audience rows and are not deleted by that path.

Main sets `SANDBOX_JOIN_ENABLED` to false, so `JOIN` creates no contact there. `JOIN DRIVER` runs only when `AUDIENCE_JOIN_ENABLED` is true. That flag is false on main and on dev until it is turned on.

The desk shows `driver_label` and a masked number. It does not show the E.164.

## Consequences

A second audience phone needs a second seeded shipment. t1 ships with one shipment, so the second join reports the pool full until more shipments exist. The join link is `https://wa.me/<digits>?text=JOIN%20DRIVER`. The digits are the WhatsApp business number and are not stored in this repo. A QR code is generated from that link at show time.
