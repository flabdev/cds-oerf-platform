const ROLE_LABELS = {
  tenant_admin: "Tenant admin",
  platform_admin: "Platform admin",
  ops_user: "Ops user",
};

export function displayName(principal) {
  if (!principal?.authenticated) {
    return "Guest";
  }
  const name = typeof principal.name === "string" ? principal.name.trim() : "";
  if (name) {
    return name;
  }
  return ROLE_LABELS[principal.role] || "Signed-in user";
}

export function sessionView(principal) {
  if (!principal?.authenticated) {
    return { state: "signed-out", displayName: "Guest" };
  }
  return {
    state: "ready",
    tenantId: principal.tenantId,
    role: principal.role,
    displayName: displayName(principal),
    canAdmin: principal.role === "tenant_admin" || principal.role === "platform_admin",
    canSwitchTenant: principal.role === "platform_admin",
  };
}

export function textField(value, fallback = "?") {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

export function shipmentMark(shipmentId) {
  const text = textField(shipmentId, "");
  return text ? text.slice(0, 1).toUpperCase() : "?";
}

const TERMINAL = new Set(["CANCELLED", "CLOSED"]);

export function isTerminal(state) {
  return TERMINAL.has(String(state || "").trim().toUpperCase());
}

export function terminalNote(state) {
  const name = String(state || "").trim().toUpperCase();
  if (name === "CANCELLED") {
    return "This incident is cancelled";
  }
  if (name === "CLOSED") {
    return "This incident is closed";
  }
  return "";
}

export function openIncidents(incidents) {
  if (!Array.isArray(incidents)) {
    return [];
  }
  return incidents.filter((item) => !isTerminal(item?.state));
}

export function incidentListView(incidents) {
  if (incidents == null) {
    return { state: "loading" };
  }
  if (incidents.error) {
    return { state: "error", message: incidents.error };
  }
  const open = openIncidents(incidents);
  if (open.length === 0) {
    return { state: "empty" };
  }
  return { state: "ready", incidents: open };
}

export function oneDecimal(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  return (Math.round(value * 10) / 10).toFixed(1);
}

const ACTION_NAMES = {
  cancel: "Cancel",
  confirm_transfer: "Confirm transfer",
  pick_option: "Pick option",
};

export function needsWaitingEngagement(action) {
  return action === "confirm_transfer" || action === "pick_option";
}

export function actionNotice(action, { queued = false, missingEngagement = false } = {}) {
  const name = ACTION_NAMES[action] || "Action";
  if (missingEngagement) {
    return `${name}: no waiting engagement for this shipment`;
  }
  return queued ? `${name} queued` : `${name} was not queued`;
}

const CHART_LEFT = 22;
const CHART_RIGHT = 112;
const CHART_MIN_GAP = 14;

export function temperatureChart(readings, band) {
  const range = numericBand(band);
  const series = (Array.isArray(readings) ? readings : [])
    .map((row) => ({
      ts: typeof row?.ts === "string" ? row.ts : "",
      temperature_c: Number(row?.temperature_c),
    }))
    .filter((row) => row.ts && Number.isFinite(row.temperature_c));
  const temps = series.map((row) => row.temperature_c);
  const floor = range ? range[0] : (temps.length ? Math.min(...temps) : 0);
  const ceiling = range ? range[1] : (temps.length ? Math.max(...temps) : 1);
  const tickValues = yTicks(temps, floor, ceiling, Boolean(range));
  const min = Math.min(...tickValues);
  const max = Math.max(...tickValues);
  const span = max - min || 1;
  const plot = (temperature) => 4 + ((max - temperature) / span) * 40;
  const xs = chartXs(series);
  const points = series.map((row, index) => ({
    x: round(xs[index]),
    y: round(plot(row.temperature_c)),
    ts: row.ts,
    temperature_c: row.temperature_c,
  }));
  const ticks = tickValues.map((value) => ({
    label: trimNumber(value),
    y: round(plot(value)),
  }));
  return {
    unit: "°C",
    band: range,
    points,
    polyline: points.map((point) => `${point.x},${point.y}`).join(" "),
    bandY: range ? round(plot(range[1])) : 0,
    bandHeight: range ? round(Math.max(plot(range[0]) - plot(range[1]), 0)) : 0,
    ticks,
    empty: points.length === 0,
  };
}

function numericBand(band) {
  if (!Array.isArray(band) || band.length !== 2) {
    return null;
  }
  const low = Number(band[0]);
  const high = Number(band[1]);
  if (!Number.isFinite(low) || !Number.isFinite(high) || low >= high) {
    return null;
  }
  return [low, high];
}

function yTicks(temps, low, high, includeBand) {
  const dataMin = temps.length ? Math.min(low, ...temps) : low;
  const dataMax = temps.length ? Math.max(high, ...temps) : high;
  const span = Math.max(dataMax - dataMin, 1);
  const step = span <= 16 ? 2 : span <= 40 ? 5 : 10;
  const start = Math.floor(dataMin / step) * step;
  const end = Math.ceil(dataMax / step) * step;
  const values = new Set(includeBand ? [low, high] : []);
  for (let value = start; value <= end + step * 0.001; value += step) {
    values.add(Math.round(value * 10) / 10);
  }
  return [...values].sort((left, right) => right - left);
}

function chartXs(series) {
  const width = CHART_RIGHT - CHART_LEFT;
  if (series.length === 0) {
    return [];
  }
  if (series.length === 1) {
    return [(CHART_LEFT + CHART_RIGHT) / 2];
  }
  const times = series.map((row) => Date.parse(row.ts));
  const timed = times.every((time) => Number.isFinite(time));
  let raw;
  if (timed) {
    const start = times[0];
    const end = times[times.length - 1];
    const span = Math.max(end - start, 0);
    const pad = Math.max(span * 0.12, 60_000);
    const domain = span + pad * 2;
    raw = times.map((time) => CHART_LEFT + ((time - (start - pad)) / domain) * width);
  } else {
    raw = series.map((_, index) => CHART_LEFT + (index / (series.length - 1)) * width);
  }
  const placed = raw.slice();
  for (let index = 1; index < placed.length; index += 1) {
    const gap = placed[index - 1] + CHART_MIN_GAP;
    if (placed[index] < gap) {
      placed[index] = gap;
    }
  }
  const overflow = placed[placed.length - 1] - CHART_RIGHT;
  if (overflow > 0) {
    const used = placed[placed.length - 1] - placed[0];
    const scale = used > width ? width / used : 1;
    const origin = placed[0];
    for (let index = 0; index < placed.length; index += 1) {
      placed[index] = CHART_LEFT + (placed[index] - origin) * scale;
    }
  }
  return placed;
}

export function incidentView(item) {
  const evidence = item?.evidence && typeof item.evidence === "object" ? item.evidence : {};
  const codes = Array.isArray(evidence.fault_codes)
    ? evidence.fault_codes.filter((code) => typeof code === "string" && code.trim())
    : [];
  const fault = typeof evidence.fault_code === "string" ? evidence.fault_code.trim() : "";
  const recommendation = item?.recommendation;
  const recommended = typeof recommendation === "string"
    ? recommendation
    : recommendation?.recommended_option;
  const options = (Array.isArray(item?.options) && item.options.length
    ? item.options
    : (Array.isArray(recommendation?.options) ? recommendation.options : []))
    .filter((option) => option && typeof option.id === "string")
    .map((option) => ({
      ...option,
      spareLabel: oneDecimal(option.spare_minutes),
      projectedLabel: oneDecimal(option.projected_minutes),
    }));
  const remaining = oneDecimal(item?.allowance_remaining);
  const readings = chartReadings(item);
  const band = numericBand(item?.range_c);
  const recommendationLabel = typeof recommended === "string" && recommended.trim()
    ? recommended.trim()
    : "—";
  return {
    allowanceLabel: remaining ? `${remaining} min` : "—",
    evidenceLabel: codes.length ? codes.join(", ") : (fault || "none"),
    recommendationLabel,
    options,
    timeline: Array.isArray(item?.timeline) ? item.timeline.filter((entry) => typeof entry === "string") : [],
    chart: temperatureChart(readings, band),
    engagementStatus: typeof item?.engagement_status === "string" ? item.engagement_status.trim() : "",
  };
}

function chartReadings(item) {
  if (Array.isArray(item?.readings) && item.readings.length) {
    return item.readings;
  }
  if (!Array.isArray(item?.temperatures)) {
    return [];
  }
  return item.temperatures.map((temperature, index) => ({
    ts: `sample-${index}`,
    temperature_c: temperature,
  }));
}

function round(value) {
  return Math.round(value * 10) / 10;
}

function trimNumber(value) {
  return String(Math.round(value * 10) / 10);
}

export function actionRequest(incidentId, action, optionId, actionId, engagementId) {
  const body = { action, action_id: actionId };
  if (engagementId) {
    body.engagement_id = engagementId;
  }
  if (optionId) {
    body.option_id = optionId;
  }
  return {
    method: "POST",
    path: `/incidents/${incidentId}/actions`,
    idempotencyKey: actionId,
    body,
  };
}

export function allowanceDraft(published, allowance) {
  const next = structuredClone(published);
  next.product_profiles[0].excursion_allowance_min = Number(allowance);
  return next;
}

export function rankingNote(allowance) {
  if (Number(allowance) <= 30) {
    return "At 30 minutes, facility B is outside the allowance.";
  }
  return "At 45 minutes, facility B is inside the allowance.";
}
