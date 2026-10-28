import assert from "node:assert/strict";
import test from "node:test";

import { formatClock, formatMinutes, formatTemperature } from "./utils/format.js";

test("minutes are whole numbers, or one decimal under ten", () => {
  assert.equal(formatMinutes(24), "24");
  assert.equal(formatMinutes(24.4), "24");
  assert.equal(formatMinutes(8.64), "8.6");
  assert.equal(formatMinutes(8), "8.0");
  assert.equal(formatMinutes(9.96), "10");
  assert.equal(formatMinutes(null), null);
});

test("temperatures keep one decimal and the unit", () => {
  assert.equal(formatTemperature(8.36), "8.4 °C");
  assert.equal(formatTemperature(8), "8.0 °C");
  assert.equal(formatTemperature("8.4"), null);
});

test("a timestamp is local HH:mm with the zone, and the title is relative", () => {
  const clock = formatClock(
    "2026-10-28T13:49:00Z",
    Date.parse("2026-10-28T13:52:00Z"),
    "Asia/Kolkata",
  );
  assert.equal(clock.time, "19:19");
  assert.ok(clock.zone);
  assert.equal(clock.label, `19:19 ${clock.zone}`);
  assert.equal(clock.title, "3 min ago");
});
