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

export function incidentListView(incidents) {
  if (incidents == null) {
    return { state: "loading" };
  }
  if (incidents.error) {
    return { state: "error", message: incidents.error };
  }
  if (incidents.length === 0) {
    return { state: "empty" };
  }
  return { state: "ready", incidents };
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

export function temperatureChart(readings, band = [2, 8]) {
  const low = Number(band?.[0]);
  const high = Number(band?.[1]);
  const range = [Number.isFinite(low) ? low : 2, Number.isFinite(high) ? high : 8];
  const series = (Array.isArray(readings) ? readings : [])
    .map((row) => ({
      ts: typeof row?.ts === "string" ? row.ts : "",
      temperature_c: Number(row?.temperature_c),
    }))
    .filter((row) => row.ts && Number.isFinite(row.temperature_c));
  const temps = series.map((row) => row.temperature_c);
  const min = Math.min(range[0], ...(temps.length ? temps : [range[0]])) - 1;
  const max = Math.max(range[1], ...(temps.length ? temps : [range[1]])) + 1;
  const span = max - min || 1;
  const plot = (temperature) => 4 + ((max - temperature) / span) * 40;
  const points = series.map((row, index) => {
    const x = 22 + (series.length === 1 ? 50 : (index / (series.length - 1)) * 100);
    return {
      x: round(x),
      y: round(plot(row.temperature_c)),
      ts: row.ts,
      temperature_c: row.temperature_c,
    };
  });
  const ticks = [max, range[1], range[0], min].map((value) => ({
    label: trimNumber(value),
    y: round(plot(value)),
  }));
  return {
    unit: "°C",
    band: range,
    points,
    polyline: points.map((point) => `${point.x},${point.y}`).join(" "),
    bandY: round(plot(range[1])),
    bandHeight: round(Math.max(plot(range[0]) - plot(range[1]), 0)),
    ticks,
    empty: points.length === 0,
  };
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
  const options = Array.isArray(item?.options) && item.options.length
    ? item.options
    : (Array.isArray(recommendation?.options) ? recommendation.options : []);
  const remaining = item?.allowance_remaining;
  const readings = chartReadings(item);
  const band = Array.isArray(item?.range_c) && item.range_c.length === 2 ? item.range_c : [2, 8];
  const recommendationLabel = typeof recommended === "string" && recommended.trim()
    ? recommended.trim()
    : "—";
  return {
    allowanceLabel: typeof remaining === "number" && Number.isFinite(remaining)
      ? `${trimNumber(remaining)} min`
      : "—",
    evidenceLabel: codes.length ? codes.join(", ") : (fault || "none"),
    recommendationLabel,
    options: options.filter((option) => option && typeof option.id === "string"),
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
