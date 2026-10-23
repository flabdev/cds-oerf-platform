/** Accept only the frozen dashboard events. Legacy broadcast shapes are ignored. */

const SUMMARY_KEYS = ["incident_id", "stage", "state", "version"];
const UPDATE_KEYS = new Set([
  "config_version",
  "from_state",
  "incident_id",
  "latest_reading",
  "shipment_id",
  "stage",
  "state",
  "timeline",
  "to_state",
  "updated_at",
  "version",
]);
const PRIVATE_KEYS = new Set([
  "email",
  "e164",
  "evidence_analysis",
  "model",
  "name",
  "phone",
  "prompt",
  "task_token",
]);

export function subscribeBody(scope, incidentId) {
  if (scope === "incident") {
    return { scope: "incident", incident_id: incidentId };
  }
  return { scope: "tenant" };
}

export function acceptDeskEvent(raw, binding) {
  const message = parse(raw);
  if (!message || message.tenant_id !== binding.tenantId) {
    return null;
  }
  if (message.event_type === "INCIDENT_SUMMARY" && binding.scope === "tenant") {
    if (!isSummary(message.data) || message.data.incident_id !== message.incident_id) {
      return null;
    }
    return { kind: "summary", incident: message.data };
  }
  if (
    message.event_type === "INCIDENT_UPDATE" &&
    binding.scope === "incident" &&
    message.incident_id === binding.incidentId
  ) {
    if (!isUpdate(message.data, binding.incidentId) || hasPrivate(message)) {
      return null;
    }
    return { kind: "update", incident: message.data };
  }
  return null;
}

export function mergeDeskRows(current, accepted) {
  if (!Array.isArray(current) || !accepted) {
    return current;
  }
  const row = accepted.incident;
  const index = current.findIndex((item) => item.incident_id === row.incident_id);
  if (index < 0) {
    return [...current, { ...row, time: "" }];
  }
  const next = current.slice();
  next[index] = { ...current[index], ...row };
  return next;
}

function parse(raw) {
  try {
    const message = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (!message || typeof message !== "object" || Array.isArray(message)) {
      return null;
    }
    return message;
  } catch {
    return null;
  }
}

function isSummary(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return false;
  }
  const keys = Object.keys(data).sort();
  if (keys.join(",") !== SUMMARY_KEYS.join(",")) {
    return false;
  }
  return (
    text(data.incident_id) &&
    text(data.state) &&
    text(data.stage) &&
    Number.isInteger(data.version)
  );
}

function isUpdate(data, incidentId) {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return false;
  }
  if (data.incident_id !== incidentId || !text(data.state) || !Number.isInteger(data.version)) {
    return false;
  }
  if (Object.keys(data).some((key) => !UPDATE_KEYS.has(key))) {
    return false;
  }
  if (data.timeline !== undefined && !validTimeline(data.timeline)) {
    return false;
  }
  if (data.latest_reading !== undefined && !validReading(data.latest_reading)) {
    return false;
  }
  return !hasPrivate(data);
}

function validTimeline(timeline) {
  if (!Array.isArray(timeline)) {
    return false;
  }
  return timeline.every((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
      return false;
    }
    return Object.keys(entry).every((key) => key === "type" || key === "at" || key === "state");
  });
}

function validReading(reading) {
  if (!reading || typeof reading !== "object" || Array.isArray(reading)) {
    return false;
  }
  return Object.keys(reading).every((key) => key === "ts" || key === "temperature_c");
}

function hasPrivate(value) {
  if (!value || typeof value !== "object") {
    return false;
  }
  if (Array.isArray(value)) {
    return value.some((item) => hasPrivate(item));
  }
  return Object.entries(value).some(([key, item]) => PRIVATE_KEYS.has(key) || hasPrivate(item));
}

function text(value) {
  return typeof value === "string" && value.trim() !== "";
}
