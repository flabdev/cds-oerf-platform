/** Incident state → label, tone, and stage. Engagement status is not a state. */

export const STAGES = [
  { id: "detect", label: "Detect" },
  { id: "understand", label: "Understand" },
  { id: "assess", label: "Assess" },
  { id: "coordinate", label: "Coordinate" },
  { id: "act", label: "Act" },
  { id: "verify", label: "Verify" },
  { id: "close", label: "Close" },
];

const STAGE_INDEX = new Map(STAGES.map((stage, index) => [stage.id, index]));

/** Tones: neutral, info, amber, green, red, grey. Cancelled is never green. */
const STATES = {
  OPEN: { label: "Open", tone: "neutral", stage: "detect" },
  DIAGNOSING: { label: "Diagnosing", tone: "info", stage: "understand" },
  ASSESSING: { label: "Assessing", tone: "info", stage: "assess" },
  AWAITING_APPROVAL: { label: "Awaiting approval", tone: "info", stage: "assess" },
  SECURING_FACILITY: { label: "Securing facility", tone: "info", stage: "coordinate" },
  IN_TRANSIT: { label: "In transit", tone: "amber", stage: "act" },
  VERIFYING: { label: "Verifying", tone: "amber", stage: "verify" },
  CONTAINED: { label: "Contained", tone: "green", stage: "verify" },
  REOPENED: { label: "Reopened", tone: "amber", stage: "verify" },
  AWAITING_DISPOSITION: { label: "Awaiting disposition", tone: "amber", stage: "close" },
  CLOSED: { label: "Closed", tone: "green", stage: "close" },
  ESCALATED: { label: "Escalated", tone: "red", stage: null },
  CANCELLED: { label: "Cancelled", tone: "grey", stage: null },
};

const PICK_STATES = new Set(["ASSESSING", "AWAITING_APPROVAL", "SECURING_FACILITY"]);
const CONFIRM_STATES = new Set(["IN_TRANSIT", "VERIFYING"]);

export function incidentState(value) {
  return String(value || "").trim().toUpperCase();
}

export function stateMeta(value) {
  const name = incidentState(value);
  if (STATES[name]) {
    return STATES[name];
  }
  return { label: name || "Unknown", tone: "neutral", stage: null };
}

export function readTransitions(item) {
  if (Array.isArray(item?.transitions) && item.transitions.length) {
    return item.transitions.map(normalizeTransition).filter(Boolean);
  }
  if (!Array.isArray(item?.timeline)) {
    return [];
  }
  return item.timeline.map(parseTimelineEntry).filter(Boolean);
}

export function stageProgress(item) {
  const name = incidentState(item?.state);
  const meta = stateMeta(name);
  const frozen = name === "CANCELLED" || name === "ESCALATED" ? name : "";
  const fromTimeline = reachedIndex(readTransitions(item));
  const fromState = meta.stage ? STAGE_INDEX.get(meta.stage) : -1;
  const index = frozen ? fromTimeline : (fromState >= 0 ? fromState : fromTimeline);
  const marker = frozen === "CANCELLED" ? "cancelled" : frozen === "ESCALATED" ? "escalated" : "current";
  const stages = STAGES.map((stage, stageIndex) => ({
    id: stage.id,
    label: stage.label,
    status: statusAt(stageIndex, index, marker),
  }));
  const here = index >= 0 ? STAGES[index].label : "";
  let banner = "";
  if (frozen === "CANCELLED") {
    banner = here ? `Cancelled at ${here}` : "Cancelled";
  } else if (frozen === "ESCALATED") {
    banner = here ? `Escalated at ${here}` : "Escalated";
  }
  return { stages, banner, activeLabel: here };
}

export function visibleActions(item) {
  const name = incidentState(item?.state);
  if (name === "CLOSED" || name === "CANCELLED" || !name) {
    return { pick: false, confirm: false, cancel: false };
  }
  const engagement = typeof item?.engagement_id === "string" && item.engagement_id.trim()
    ? item.engagement_id.trim()
    : "";
  const status = incidentState(item?.engagement_status);
  const waiting = Boolean(engagement) && (status === "" || status === "WAITING");
  return {
    pick: PICK_STATES.has(name),
    confirm: CONFIRM_STATES.has(name) && waiting,
    cancel: true,
  };
}

function reachedIndex(transitions) {
  let reached = -1;
  for (const entry of transitions) {
    reached = Math.max(reached, stageIndex(entry?.from_state), stageIndex(entry?.to_state));
  }
  return reached;
}

function stageIndex(value) {
  const stage = stateMeta(value).stage;
  if (!stage || !STAGE_INDEX.has(stage)) {
    return -1;
  }
  return STAGE_INDEX.get(stage);
}

function statusAt(stageIndex, current, marker) {
  if (current < 0) {
    return "future";
  }
  if (stageIndex < current) {
    return "done";
  }
  if (stageIndex === current) {
    return marker;
  }
  return "future";
}

function normalizeTransition(entry) {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    return null;
  }
  const from = text(entry.from_state);
  const to = text(entry.to_state) || text(entry.state);
  if (!from && !to) {
    return null;
  }
  return { at: text(entry.at), from_state: from, to_state: to };
}

function parseTimelineEntry(entry) {
  const structured = normalizeTransition(entry);
  if (structured) {
    return structured;
  }
  if (typeof entry !== "string") {
    return null;
  }
  const arrow = entry.match(/(\d{2}:\d{2})?\s*([A-Za-z_]+)?\s*→\s*([A-Za-z_]+)/);
  if (arrow) {
    return {
      at: arrow[1] || "",
      from_state: arrow[2] ? arrow[2].toUpperCase() : "",
      to_state: arrow[3].toUpperCase(),
    };
  }
  const words = entry.match(/(\d{2}:\d{2})\s+([A-Za-z_]+)/);
  if (!words) {
    return null;
  }
  const token = words[2].toUpperCase();
  return {
    at: words[1],
    from_state: "",
    to_state: token === "OPENED" ? "OPEN" : token,
  };
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}
