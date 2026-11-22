/** Silent REST fallback when the socket is quiet. Does not drive the desk badge. */

export const DESK_QUIET_MS = 10_000;

export function initialDeskRefresh(openedAt) {
  return { lastMessageAt: null, nextPollAt: openedAt + DESK_QUIET_MS };
}

export function noteDeskMessage(state, at) {
  return { lastMessageAt: at, nextPollAt: at + DESK_QUIET_MS };
}

/** The next poll state when one is due, otherwise null. */
export function takeDeskPoll(state, now) {
  if (!state || now < state.nextPollAt) {
    return null;
  }
  return { ...state, nextPollAt: now + DESK_QUIET_MS };
}

export function socketPresenceLabel(presence) {
  if (presence === "live") {
    return "Live";
  }
  if (presence === "reconnecting") {
    return "Reconnecting";
  }
  return "";
}

/** Live when every tracked socket is open; reconnecting otherwise. */
export function socketPresenceFromRefs(sockets) {
  if (!Array.isArray(sockets) || sockets.length === 0) {
    return "reconnecting";
  }
  if (sockets.every((socket) => socket.readyState === WebSocket.OPEN)) {
    return "live";
  }
  return "reconnecting";
}