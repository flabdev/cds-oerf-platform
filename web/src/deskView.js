/** Detail view-model. Every number comes from the incident payload or a stated estimate. */

import { formatClock, formatDuration, formatLimit, formatMinutes, formatTemperature } from "./utils/format.js";
import { incidentState, readTransitions, stateMeta, visibleActions } from "./states.js";

const PLOT = { width: 520, height: 200, padL: 44, padR: 16, padT: 22, padB: 32 };

export function buildDesk(item, now = Date.now()) {
  const transitions = readTransitions(item);
  const band = numericBand(item?.range_c) || numericBand(profileOf(item)?.range_c);
  const readings = chartReadings(item);
  const profile = profileOf(item);
  const total = allowanceTotal(item, profile);
  const above = band ? minutesSinceAbove(readings, band[1], now) : null;
  const apiRemaining = finite(item?.allowance_remaining);
  const estimated = apiRemaining == null && total != null && above != null ? total - above : null;
  const remaining = apiRemaining != null ? apiRemaining : estimated;
  const disagrees = apiRemaining != null && estimated == null && total != null && above != null
    && formatMinutes(apiRemaining) !== formatMinutes(total - above);
  const recommendationId = recommendedId(item);
  const chosenId = chosenOptionId(item);
  const options = optionCards(item, total, recommendationId, chosenId);
  return {
    shipmentId: text(item?.shipment_id),
    profileName: text(profile?.name),
    rangeLabel: band ? `${formatLimit(band[0]).replace(" °C", "")}–${formatLimit(band[1])}` : "",
    opened: formatClock(item?.opened_at, now),
    actions: visibleActions(item),
    terminalLabel: closedLabel(item, transitions, now),
    recommendationId,
    kpis: {
      temperature: temperatureKpi(readings, band),
      above: aboveKpi(above, band),
      allowance: allowanceKpi(remaining, total, apiRemaining == null && estimated != null),
      evidence: evidenceKpi(item),
    },
    allowanceDisagrees: Boolean(disagrees),
    chart: temperaturePlot(readings, band, {
      openedAt: item?.opened_at,
      containedAt: containedAt(transitions),
    }, now),
    options,
    why: whyRows(options, above, total),
    feed: feedItems(item, transitions, options, recommendationId, now),
    aboveLimit: aboveLimit(readings, band),
    elapsed: elapsedLabel(item, now),
  };
}

export function aboveLimit(readings, band) {
  if (!band || !readings.length) {
    return false;
  }
  const last = Number(readings[readings.length - 1].temperature_c);
  return Number.isFinite(last) && last > band[1];
}

export function temperaturePlot(readings, band, markers = {}, now = Date.now()) {
  const series = (Array.isArray(readings) ? readings : [])
    .map((row) => ({
      ts: typeof row?.ts === "string" ? row.ts : "",
      time: Date.parse(row?.ts),
      temperature_c: Number(row?.temperature_c),
    }))
    .filter((row) => Number.isFinite(row.temperature_c) && Number.isFinite(row.time));
  if (!series.length) {
    return { empty: true, yLabel: "°C", width: PLOT.width, height: PLOT.height };
  }
  const range = numericBand(band);
  const temps = series.map((row) => row.temperature_c);
  const yMin = Math.min(range ? range[0] : temps[0], ...temps) - 1;
  const yMax = Math.max(range ? range[1] : temps[0], ...temps) + 1;
  const markerTimes = [markers.openedAt, markers.containedAt]
    .map((value) => Date.parse(value))
    .filter((value) => Number.isFinite(value));
  const times = series.map((row) => row.time).concat(markerTimes);
  const xMin = Math.min(...times);
  const xMax = Math.max(...times);
  const yOf = (temperature) => {
    const span = yMax - yMin || 1;
    return PLOT.padT + ((yMax - temperature) / span) * (PLOT.height - PLOT.padT - PLOT.padB);
  };
  const xOf = (time) => {
    const span = xMax - xMin || 1;
    const plotW = PLOT.width - PLOT.padL - PLOT.padR;
    return PLOT.padL + ((time - xMin) / span) * plotW;
  };
  const points = series.map((row) => {
    const clock = formatClock(row.ts, now);
    const temperature = formatTemperature(row.temperature_c);
    const hot = Boolean(range) && row.temperature_c > range[1];
    return {
      x: round(xOf(row.time)),
      y: round(yOf(row.temperature_c)),
      hot,
      title: [clock?.label, temperature, clock?.title, hot ? "above the limit" : ""].filter(Boolean).join(" · "),
    };
  });
  const ticks = [];
  for (let value = Math.ceil(yMin / 2) * 2; value <= yMax + 1e-6; value += 2) {
    ticks.push({ value, y: round(yOf(value)), label: String(value) });
  }
  const zone = formatClock(series[0].ts, now)?.zone || "";
  const xLabels = labelIndexes(points.length).map((index) => ({
    x: points[index].x,
    label: formatClock(series[index].ts, now)?.time || "",
  }));
  const plotMarkers = [];
  if (Number.isFinite(Date.parse(markers.openedAt))) {
    plotMarkers.push({ x: round(xOf(Date.parse(markers.openedAt))), label: "Opened" });
  }
  if (Number.isFinite(Date.parse(markers.containedAt))) {
    plotMarkers.push({ x: round(xOf(Date.parse(markers.containedAt))), label: "Contained" });
  }
  return {
    empty: false,
    yLabel: "°C",
    zone,
    width: PLOT.width,
    height: PLOT.height,
    yMin: round(yMin),
    yMax: round(yMax),
    ticks,
    xLabels,
    points,
    line: points.map((point) => `${point.x},${point.y}`).join(" "),
    band: range ? {
      y: round(yOf(range[1])),
      height: round(Math.max(yOf(range[0]) - yOf(range[1]), 0)),
      x: PLOT.padL,
      width: PLOT.width - PLOT.padL - PLOT.padR,
    } : null,
    threshold: range ? { y: round(yOf(range[1])), x1: PLOT.padL, x2: PLOT.width - PLOT.padR } : null,
    markers: plotMarkers,
    label: ariaLabel(series, range),
  };
}

function feedItems(item, transitions, options, recommendationId, now) {
  const items = [];
  if (text(item?.opened_at)) {
    items.push(timed("state", item.opened_at, "Incident opened", now));
  }
  for (const entry of transitions) {
    const from = entry.from_state ? stateMeta(entry.from_state).label : "";
    const to = entry.to_state ? stateMeta(entry.to_state).label : "";
    if (!to || incidentState(entry.to_state) === "OPEN") {
      continue;
    }
    const label = from && from !== to ? `${from} → ${to}` : to;
    items.push(timed("state", entry.at, label, now));
  }
  const decision = decisionItem(item, transitions, options, recommendationId, now);
  if (decision) {
    items.push(decision);
  }
  const agent = agentItem(item, now);
  if (agent) {
    items.push(agent);
  }
  const messages = messageItems(item, now);
  if (messages.length) {
    items.push(...messages);
  } else {
    const engagement = engagementItem(item);
    if (engagement) {
      items.push(engagement);
    }
  }
  return items
    .map((entry, index) => ({ ...entry, id: entry.id || `${entry.kind}-${index}-${entry.text}` }))
    .sort((left, right) => compareTime(left.at, right.at));
}

function decisionItem(item, transitions, options, recommendationId, now) {
  if (!recommendationId) {
    return null;
  }
  const match = options.find((option) => option.id === recommendationId);
  const name = match?.name || recommendationId;
  const spare = match?.spareMinutes;
  const spareText = spare == null ? "" : ` (${formatMinutes(spare)} min spare)`;
  const at = transitions.find((entry) => incidentState(entry.to_state) === "ASSESSING")?.at || "";
  return timed("decision", at, `Rules recommended ${name}${spareText}`, now);
}

function agentItem(item, now) {
  const source = item?.interpretation && typeof item.interpretation === "object"
    ? item.interpretation
    : null;
  const understood = source ? firstString(source.summary, source.text) : "";
  if (!understood) {
    return null;
  }
  const entry = timed("agent", source.at || "", `Agent understood: ${understood}${confidenceSuffix(source.confidence)}`, now);
  if (source.fallback === true || source.advisory === true) {
    entry.tag = "advisory";
  }
  return entry;
}

function messageItems(item, now) {
  if (!Array.isArray(item?.messages)) {
    return [];
  }
  return item.messages.flatMap((row, index) => {
    if (!row || typeof row !== "object") {
      return [];
    }
    const body = firstString(row.text, row.body, row.summary, row.label);
    if (!body) {
      return [];
    }
    const inbound = String(row.direction || "").toLowerCase() === "inbound";
    const channel = channelName(row.channel);
    const role = firstString(row.role_label, row.role);
    const rung = firstString(row.rung, row.template);
    const status = firstString(row.delivery_status, row.status);
    const bits = [channel, role, rung, status].filter(Boolean);
    const text = inbound
      ? [role, body].filter(Boolean).join(": ")
      : [bits.join(" · "), body].filter(Boolean).join(" — ");
    const entry = timed(inbound ? "inbound" : "outbound", row.at || "", text, now);
    entry.id = `msg-${index}`;
    return [entry];
  });
}

function engagementItem(item) {
  const id = text(item?.engagement_id);
  const status = text(item?.engagement_status);
  if (!id && !status) {
    return null;
  }
  const pretty = status ? status.charAt(0).toUpperCase() + status.slice(1).toLowerCase() : "";
  const label = [id ? `Engagement ${id}` : "Engagement", pretty].filter(Boolean).join(" · ");
  return { kind: "engagement", at: "", time: "", title: "", text: label };
}

function optionCards(item, total, recommendationId, chosenId) {
  const raw = Array.isArray(item?.options) && item.options.length
    ? item.options
    : (Array.isArray(item?.recommendation?.options) ? item.recommendation.options : []);
  const cards = raw
    .filter((option) => option && typeof option.id === "string" && option.id.trim())
    .map((option) => {
      const id = option.id.trim();
      const spare = finite(option.spare_minutes);
      const projected = finite(option.projected_minutes);
      const eta = finite(option.eta_min) ?? finite(option.drive_min) ?? finite(option.eta);
      const reason = firstString(option.reason, option.infeasible_reason);
      const ratio = spare != null && total != null && total > 0
        ? Math.max(0, Math.min(1, spare / total))
        : null;
      return {
        id,
        name: optionName(option, item),
        spareMinutes: spare,
        spareLabel: spare == null ? "" : `${formatMinutes(spare)} min spare`,
        projectedLabel: projected == null ? "" : `projected ${formatMinutes(projected)} min`,
        etaLabel: eta == null ? "" : `ETA ${formatMinutes(eta)} min`,
        feasible: option.feasible === true,
        reason: reason || "",
        recommended: id === recommendationId,
        selected: id === chosenId,
        bar: ratio,
      };
    });
  const feasible = cards.filter((option) => option.feasible);
  const infeasible = cards.filter((option) => !option.feasible);
  return [...feasible, ...infeasible];
}

function whyRows(options, above, total) {
  if (!options.length) {
    return [];
  }
  return options.map((option) => ({
    id: option.id,
    name: option.name,
    above: above == null ? "" : formatDuration(above),
    allowance: total == null ? "" : `${formatMinutes(total)} min`,
    spare: option.spareLabel,
    eta: option.etaLabel,
    projected: option.projectedLabel,
  }));
}

function optionName(option, item) {
  const id = option.id.trim();
  if (id.toLowerCase() === "continue" || option.kind === "continue") {
    return "Continue to destination";
  }
  const named = facilityName(item, id);
  return named || id;
}

function facilityName(item, id) {
  const pools = []
    .concat(Array.isArray(item?.facilities) ? item.facilities : [])
    .concat(Array.isArray(item?.config_snapshot?.facilities) ? item.config_snapshot.facilities : []);
  const found = pools.find((facility) => {
    if (!facility || typeof facility !== "object") {
      return false;
    }
    return facility.id === id || facility.facility_id === id;
  });
  return found ? text(found.name) : "";
}

function temperatureKpi(readings, band) {
  if (!readings.length) {
    return { value: "No reading", hot: false };
  }
  const last = Number(readings[readings.length - 1].temperature_c);
  const hot = Boolean(band) && Number.isFinite(last) && (last > band[1] || last < band[0]);
  return { value: formatTemperature(last) || "No reading", hot };
}

function aboveKpi(minutes, band) {
  const title = band ? `Above ${formatLimit(band[1])}` : "Above the limit";
  if (minutes == null) {
    return { title, value: "—" };
  }
  return { title, value: formatDuration(minutes) };
}

function allowanceKpi(remaining, total, estimated) {
  if (remaining == null) {
    return { value: "—", detail: "", ratio: null, estimated: false };
  }
  const left = `${formatMinutes(remaining)} min left`;
  if (total == null || total <= 0) {
    return { value: left, detail: estimated ? "est." : "", ratio: null, estimated };
  }
  const ratio = Math.max(0, Math.min(1, remaining / total));
  return {
    value: `${formatMinutes(remaining)} / ${formatMinutes(total)} min left`,
    detail: estimated ? "est." : "",
    ratio,
    estimated,
  };
}

function evidenceKpi(item) {
  const source = item?.evidence && typeof item.evidence === "object" ? item.evidence : {};
  const photos = Array.isArray(source.photos) ? source.photos.length : 0;
  if (photos) {
    return { value: photos === 1 ? "1 photo" : `${photos} photos` };
  }
  const codes = faultCodes(source);
  if (!codes.length) {
    return { value: "None" };
  }
  const labels = faultLabels(item, source);
  return { value: codes.map((code) => labeledFault(code, labels)).join(", ") };
}

function faultCodes(source) {
  const raw = Array.isArray(source.fault_codes) ? source.fault_codes : [];
  const codes = [];
  for (const entry of raw) {
    const code = faultCode(entry);
    if (code) {
      codes.push(code);
    }
  }
  if (!codes.length) {
    const single = faultCode(source.fault_code);
    if (single) {
      codes.push(single);
    }
  }
  return codes;
}

function faultCode(entry) {
  if (typeof entry === "string" && entry.trim()) {
    return entry.trim();
  }
  if (!entry || typeof entry !== "object") {
    return "";
  }
  return text(entry.code) || text(entry.fault_code) || text(entry.id);
}

function faultLabels(item, source) {
  const labels = new Map();
  absorbLabels(labels, source.labels);
  absorbLabels(labels, source.fault_labels);
  const snapshot = item?.config_snapshot;
  if (snapshot && typeof snapshot === "object") {
    absorbLabels(labels, snapshot.fault_labels);
    const catalog = snapshot.fault_codes;
    if (Array.isArray(catalog)) {
      for (const entry of catalog) {
        const code = faultCode(entry);
        const label = faultLabel(entry);
        if (code && label) {
          labels.set(code.toUpperCase(), label);
        }
      }
    }
  }
  const raw = Array.isArray(source.fault_codes) ? source.fault_codes : [];
  for (const entry of raw) {
    const code = faultCode(entry);
    const label = faultLabel(entry);
    if (code && label) {
      labels.set(code.toUpperCase(), label);
    }
  }
  return labels;
}

function absorbLabels(labels, map) {
  if (!map || typeof map !== "object" || Array.isArray(map)) {
    return;
  }
  for (const [code, label] of Object.entries(map)) {
    if (text(code) && text(label)) {
      labels.set(code.trim().toUpperCase(), label.trim());
    }
  }
}

function faultLabel(entry) {
  if (!entry || typeof entry !== "object") {
    return "";
  }
  return text(entry.label) || text(entry.description);
}

function labeledFault(code, labels) {
  const label = labels.get(code.toUpperCase());
  return label ? `${code} — ${label}` : code;
}

function closedLabel(item, transitions, now) {
  const name = incidentState(item?.state);
  if (name !== "CANCELLED" && name !== "CLOSED") {
    return "";
  }
  const verb = name === "CANCELLED" ? "Cancelled" : "Closed";
  const event = [...transitions].reverse().find((entry) => incidentState(entry.to_state) === name);
  const clock = formatClock(event?.at || item?.updated_at, now);
  if (clock) {
    return `${verb} at ${clock.label}`;
  }
  return verb;
}

function elapsedLabel(item, now) {
  const opened = Date.parse(item?.opened_at);
  if (!Number.isFinite(opened)) {
    return "";
  }
  return formatDuration(Math.max(0, (now - opened) / 60000));
}

function minutesSinceAbove(readings, max, now) {
  const points = readings.filter((row) => Number.isFinite(Date.parse(row.ts)));
  if (!points.length) {
    return null;
  }
  const first = points.find((row) => Number(row.temperature_c) > max);
  if (!first) {
    return 0;
  }
  return Math.max(0, (now - Date.parse(first.ts)) / 60000);
}

function containedAt(transitions) {
  return transitions.find((entry) => incidentState(entry.to_state) === "CONTAINED")?.at || "";
}

function profileOf(item) {
  const id = text(item?.product_profile_id);
  const profiles = item?.config_snapshot?.product_profiles;
  if (!id || !Array.isArray(profiles)) {
    return null;
  }
  return profiles.find((profile) => profile && profile.id === id) || null;
}

function allowanceTotal(item, profile) {
  return finite(profile?.excursion_allowance_min) ?? finite(item?.allowance_minutes);
}

function recommendedId(item) {
  const recommendation = item?.recommendation;
  if (typeof recommendation === "string" && recommendation.trim()) {
    return recommendation.trim();
  }
  if (recommendation && typeof recommendation.recommended_option === "string") {
    return recommendation.recommended_option.trim();
  }
  return "";
}

function chosenOptionId(item) {
  const direct = firstString(item?.chosen_option, item?.selected_option);
  if (direct) {
    return direct;
  }
  const approval = item?.approval;
  if (approval && typeof approval === "object") {
    return firstString(approval.option_id, approval.chosen_option);
  }
  return "";
}

function chartReadings(item) {
  if (Array.isArray(item?.readings) && item.readings.length) {
    return item.readings;
  }
  if (!Array.isArray(item?.temperatures)) {
    return [];
  }
  return item.temperatures.map((temperature, index) => ({
    ts: "",
    temperature_c: temperature,
    index,
  }));
}

function timed(kind, at, text, now) {
  const clock = formatClock(at, now);
  return {
    kind,
    at: typeof at === "string" ? at : "",
    time: clock?.label || (/^\d{2}:\d{2}/.test(at || "") ? at : ""),
    title: clock?.title || "",
    text,
  };
}

function compareTime(left, right) {
  const a = Date.parse(left);
  const b = Date.parse(right);
  const aOk = Number.isFinite(a);
  const bOk = Number.isFinite(b);
  if (aOk && bOk && a !== b) {
    return a - b;
  }
  if (aOk !== bOk) {
    return aOk ? -1 : 1;
  }
  return 0;
}

function ariaLabel(series, range) {
  const last = formatTemperature(series[series.length - 1].temperature_c);
  const band = range ? `, band ${formatLimit(range[0])} to ${formatLimit(range[1])}` : "";
  return `Temperature ${last}${band}`;
}

function labelIndexes(length) {
  if (length <= 4) {
    return Array.from({ length }, (_, index) => index);
  }
  return [0, Math.round((length - 1) / 2), length - 1];
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

function confidenceSuffix(value) {
  const number = finite(value);
  if (number == null) {
    return "";
  }
  const pct = number <= 1 ? number * 100 : number;
  return ` (${(Math.round(pct * 10) / 10).toFixed(1)}%)`;
}

function channelName(value) {
  const name = String(value || "").trim().toLowerCase();
  if (name === "whatsapp") return "WhatsApp";
  if (name === "sms") return "SMS";
  if (name === "voice") return "Voice";
  if (name === "email") return "Email";
  return "";
}

function finite(value) {
  const number = typeof value === "number" ? value : (typeof value === "string" && value.trim() ? Number(value) : NaN);
  return Number.isFinite(number) ? number : null;
}

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return "";
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function round(value) {
  return Math.round(value * 10) / 10;
}
