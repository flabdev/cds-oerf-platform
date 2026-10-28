/** One place for minutes, temperatures, and local clocks. */

export function formatMinutes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  const tenths = Math.round(value * 10) / 10;
  if (Math.abs(tenths) < 10) {
    return tenths.toFixed(1);
  }
  return String(Math.round(tenths));
}

export function formatTemperature(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  return `${(Math.round(value * 10) / 10).toFixed(1)} °C`;
}

/** Whole degrees stay whole. "8 °C", not "8.0 °C". */
export function formatLimit(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  if (Math.abs(value - Math.round(value)) < 1e-9) {
    return `${Math.round(value)} °C`;
  }
  return formatTemperature(value);
}

export function formatDuration(minutes) {
  if (typeof minutes !== "number" || !Number.isFinite(minutes) || minutes < 0) {
    return null;
  }
  if (minutes === 0) {
    return "0 min";
  }
  if (minutes < 60) {
    return `${formatMinutes(minutes)} min`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = Math.round(minutes % 60);
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

export function formatRelative(value, now = Date.now()) {
  const then = typeof value === "number" ? value : Date.parse(value);
  if (!Number.isFinite(then)) {
    return "";
  }
  const deltaSec = Math.round((now - then) / 1000);
  const abs = Math.abs(deltaSec);
  if (abs < 45) {
    return "just now";
  }
  const minutes = Math.round(deltaSec / 60);
  if (Math.abs(minutes) < 60) {
    return minutes >= 0 ? `${minutes} min ago` : `in ${-minutes} min`;
  }
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) {
    return hours >= 0 ? `${hours} h ago` : `in ${-hours} h`;
  }
  const days = Math.round(hours / 24);
  return days >= 0 ? `${days} d ago` : `in ${-days} d`;
}

export function formatClock(value, now = Date.now(), timeZone) {
  const date = parseStamp(value);
  if (!date) {
    return null;
  }
  const base = {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  };
  if (timeZone) {
    base.timeZone = timeZone;
  }
  const time = new Intl.DateTimeFormat("en-IN", base).format(date);
  const zone = timeZoneName(date, timeZone);
  return {
    time,
    zone,
    label: zone ? `${time} ${zone}` : time,
    title: formatRelative(date.getTime(), now),
  };
}

function parseStamp(value) {
  if (value instanceof Date && !Number.isNaN(value.getTime())) {
    return value;
  }
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  return date;
}

function timeZoneName(date, timeZone) {
  const options = { hour: "2-digit", minute: "2-digit", timeZoneName: "short" };
  if (timeZone) {
    options.timeZone = timeZone;
  }
  const parts = new Intl.DateTimeFormat("en-IN", options).formatToParts(date);
  const zone = parts.find((part) => part.type === "timeZoneName")?.value || "";
  return ZONE_ALIAS[zone.trim()] || zone.trim();
}

const ZONE_ALIAS = {
  "GMT+5:30": "IST",
  "GMT+05:30": "IST",
};
