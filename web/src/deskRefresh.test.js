import assert from "node:assert/strict";
import test from "node:test";

import {
  DESK_QUIET_MS,
  initialDeskRefresh,
  noteDeskMessage,
  socketPresenceFromRefs,
  socketPresenceLabel,
  takeDeskPoll,
} from "./deskRefresh.js";

function socket(state) {
  return { readyState: state };
}

test("the open desk polls every 10s until a socket message arrives", () => {
  let state = initialDeskRefresh(0);
  assert.equal(takeDeskPoll(state, DESK_QUIET_MS - 1), null);

  state = takeDeskPoll(state, DESK_QUIET_MS);
  assert.equal(state.nextPollAt, DESK_QUIET_MS * 2);

  state = takeDeskPoll(state, DESK_QUIET_MS * 2);
  assert.equal(state.nextPollAt, DESK_QUIET_MS * 3);
  assert.equal(takeDeskPoll(state, DESK_QUIET_MS * 3 - 1), null);
});

test("a socket message holds the next poll for 10s", () => {
  let state = noteDeskMessage(initialDeskRefresh(0), 4_000);
  assert.equal(takeDeskPoll(state, 13_999), null);

  state = takeDeskPoll(state, 14_000);
  assert.equal(state.lastMessageAt, 4_000);
  assert.equal(state.nextPollAt, 24_000);
});

test("another message while quiet postpones the poll by another 10s", () => {
  let state = noteDeskMessage(initialDeskRefresh(0), 1_000);
  state = noteDeskMessage(state, 9_000);
  assert.equal(takeDeskPoll(state, 18_999), null);

  state = takeDeskPoll(state, 19_000);
  assert.equal(state.nextPollAt, 29_000);
});

test("the badge is Live only when every socket is open", () => {
  const open = 1;
  const connecting = 0;
  assert.equal(socketPresenceLabel("live"), "Live");
  assert.equal(socketPresenceLabel("reconnecting"), "Reconnecting");
  assert.equal(socketPresenceLabel(undefined), "");
  assert.equal(socketPresenceFromRefs([]), "reconnecting");
  assert.equal(socketPresenceFromRefs([socket(connecting)]), "reconnecting");
  assert.equal(socketPresenceFromRefs([socket(open)]), "live");
  assert.equal(socketPresenceFromRefs([socket(open), socket(connecting)]), "reconnecting");
  assert.equal(socketPresenceFromRefs([socket(open), socket(open)]), "live");
});