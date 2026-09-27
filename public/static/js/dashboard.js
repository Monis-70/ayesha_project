// Dashboard: fetch JSON from the Flask API about every 5 s and draw it.
"use strict";

const REFRESH_MS = 5000;
const CHART_LIMIT = 20000;          // API maximum
const SENSOR_RGB = { temperature: [228, 87, 46], humidity: [31, 119, 180], pressure: [123, 75, 196] };
const TICK_STEPS_MIN = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440];
const PRESET_NAMES = { 15: "Last 15 minutes", 60: "Last hour", 360: "Last 6 hours", 1440: "Last 24 hours", 10080: "Last 7 days" };
const DAY_MS = 86400000;

const state = {
  sensorId: null,
  presetMinutes: 1440,              // null while a custom range is active
  customStart: null,
  customEnd: null,
  paused: false,
};

let chart = null;
let chartSensor = null;             // sensor currently drawn (used by chart callbacks)
let tickStepMs = 60000;
let timer = null;
let running = false;
let rerun = false;

const $ = (id) => document.getElementById(id);

// ---------- helpers ----------

async function requestJSON(url, options) {
  const res = await fetch(url, options);
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

const getJSON = (url) => requestJSON(url);
const postJSON = (url, data) => requestJSON(url, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
});

const rgba = (rgb, a) => `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${a})`;
const fmtNum = (v) => (v === null || v === undefined ? "–" : Number(v).toFixed(2));
const fmtInt = (v) => (v === null || v === undefined ? "–" : Number(v).toLocaleString());

function fmtDateTime(iso) {
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

// Buckets are UTC hours, so in zones like UTC+5:30 they start at :30 local time.
function fmtHourBucket(iso) {
  const start = new Date(iso);
  const end = new Date(start.getTime() + 3600 * 1000);
  const hm = { hour: "2-digit", minute: "2-digit" };
  return `${start.toLocaleString([], { month: "short", day: "numeric", ...hm })}–${end.toLocaleTimeString([], hm)}`;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function timeAgo(iso) {
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s} seconds ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${plural(m, "minute")} ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${plural(h, "hour")} ago`;
  return `${plural(Math.floor(h / 24), "day")} ago`;
}

function fmtPeriod(ms) {
  if (ms >= DAY_MS) return plural(Math.round((ms / DAY_MS) * 10) / 10, "day");
  if (ms >= 3600000) return plural(Math.round(ms / 3600000), "hour");
  return plural(Math.max(1, Math.round(ms / 60000)), "minute");
}

// Date -> value for <input type="datetime-local"> (local time, no seconds)
function toLocalInput(d) {
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// An element whose "x seconds ago" text is kept fresh by the 1-second clock.
function agoEl(tag, className, iso, prefix = "") {
  const node = el(tag, className, prefix + timeAgo(iso));
  node.dataset.ago = iso;
  node.dataset.prefix = prefix;
  node.title = new Date(iso).toLocaleString();
  return node;
}

function setAgo(node, iso, prefix = "") {
  node.dataset.ago = iso;
  node.dataset.prefix = prefix;
  node.textContent = prefix + timeAgo(iso);
}

function isBreached(sensor, value) {
  const rule = sensor.alert_rule;
  return rule.condition === ">" ? value > rule.threshold : value < rule.threshold;
}

function thresholdText(sensor) {
  return `${sensor.alert_rule.condition} ${sensor.alert_rule.threshold} ${sensor.unit}`;
}

function setValue(id, text, level) {
  const node = $(id);
  node.textContent = text;
  node.className = level ? `value ${level}` : "value";
}

// Fill a <tbody> safely with textContent (never innerHTML). Each cell is [text, className].
function fillRows(tbodyId, rows, toCells, emptyText) {
  const tbody = $(tbodyId);
  if (rows.length === 0) {
    const td = el("td", "empty", emptyText);
    td.colSpan = tbody.parentElement.tHead.rows[0].cells.length;
    const tr = el("tr");
    tr.append(td);
    tbody.replaceChildren(tr);
    return;
  }
  tbody.replaceChildren(...rows.map((r) => {
    const tr = el("tr");
    for (const [text, cls] of toCells(r)) tr.append(el("td", cls || "", String(text)));
    return tr;
  }));
}

// ---------- time range ----------

function currentRange() {
  if (state.presetMinutes === null) return { start: state.customStart, end: state.customEnd };
  const end = new Date();
  return { start: new Date(end.getTime() - state.presetMinutes * 60000), end };
}

function rangeQuery(range) {
  return new URLSearchParams({
    sensor_id: state.sensorId,
    start: range.start.toISOString(),
    end: range.end.toISOString(),
  }).toString();
}

function rangeLabel() {
  if (state.presetMinutes !== null) return PRESET_NAMES[state.presetMinutes];
  return `${state.customStart.toLocaleString()} → ${state.customEnd.toLocaleString()}`;
}

// ---------- rendering: header, status, summary ----------

function tickClock() {
  $("clock").textContent = new Date().toLocaleString([], {
    weekday: "short", day: "numeric", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  document.querySelectorAll("[data-ago]").forEach((node) => {
    node.textContent = node.dataset.prefix + timeAgo(node.dataset.ago);
  });
}

function setOnline(isOnline, message) {
  const badge = $("online");
  badge.classList.toggle("is-online", isOnline);
  badge.classList.toggle("is-offline", !isOnline);
  $("online-text").textContent = isOnline ? "Online" : "Offline";
  const banner = $("error-banner");
  banner.hidden = isOnline;
  if (!isOnline) {
    banner.textContent = `Cannot reach the API (${message}). Is "python app.py" still running?`;
    setValue("st-system", "OFFLINE", "bad");
    setValue("st-db", "UNKNOWN", "warn");
  }
}

function renderSummary(summary) {
  $("kpi-total").textContent = fmtInt(summary.sensors_total);
  $("kpi-active").textContent = `${summary.sensors_active} / ${summary.sensors_total}`;
  $("kpi-alerts").textContent = fmtInt(summary.alerts_today);
  $("kpi-readings").textContent = fmtInt(summary.readings_stored);

  const db = summary.database;
  const sim = summary.simulator;
  const demo = summary.mode === "demo";
  setValue("st-system", "ONLINE", "ok");
  if (demo) setValue("st-db", "DEMO DATA (no DB)", "ok");
  else setValue("st-db", db.connected ? "CONNECTED" : "DISCONNECTED", db.connected ? "ok" : "bad");
  setValue("st-sim", demo ? "ON-DEMAND" : sim.running ? "RUNNING" : "STOPPED", demo || sim.running ? "ok" : "warn");
  if (db.last_reading) setAgo($("st-last"), db.last_reading);
  else setValue("st-last", "no readings", "warn");
  $("st-history").textContent = db.first_reading
    ? `${fmtPeriod(Date.parse(db.last_reading) - Date.parse(db.first_reading))} (${summary.history.seed_step_minutes}-min samples)`
    : "none";
  $("st-live").textContent = `Every ${sim.interval_seconds} seconds`;

  $("db-engine").textContent = `${db.engine} ${db.version}`;
  $("db-file").textContent = db.file ? `data/${db.file}` : "none (serverless)";
  $("db-tables").textContent = db.tables ?? "not used";
  $("db-sensors").textContent = db.sensors;
  $("db-readings").textContent = fmtInt(db.readings);
  $("db-alerts").textContent = fmtInt(db.alerts);
  $("db-first").textContent = db.first_reading ? fmtDateTime(db.first_reading) : "–";
  $("db-last").textContent = db.last_reading ? fmtDateTime(db.last_reading) : "–";
}

// ---------- rendering: sensors ----------

function renderSensorSelect(sensors) {
  const select = $("sensor");
  if (select.options.length) return;
  for (const s of sensors) {
    const opt = el("option", "", `${s.sensor_name} (${s.sensor_id})`);
    opt.value = s.sensor_id;
    select.append(opt);
  }
  select.value = state.sensorId;
}

function renderCards(sensors, alertList) {
  $("cards").replaceChildren(...sensors.map((s) => {
    const classes = ["card", `type-${s.sensor_type}`];
    if (s.in_alert) classes.push("is-alert");
    if (s.sensor_id === state.sensorId) classes.push("is-selected");
    const card = el("div", classes.join(" "));
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.title = "Show this sensor in the chart";
    card.addEventListener("click", () => selectSensor(s.sensor_id));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectSensor(s.sensor_id); }
    });

    const head = el("div", "card-head");
    head.append(el("span", "card-name", s.sensor_name.toUpperCase()), el("span", "card-id", s.sensor_id));
    card.append(head);

    const value = el("div", "card-value");
    if (s.latest) value.append(el("span", "num", fmtNum(s.latest.value)), el("span", "unit", s.unit));
    else value.append(el("span", "num muted", "No data"));
    card.append(value);

    card.append(el("span", "card-status", s.in_alert ? "ALERT" : "NORMAL"));
    card.append(s.latest ? agoEl("div", "card-meta", s.latest.timestamp, "Updated ")
                         : el("div", "card-meta", "No readings stored yet"));
    card.append(el("div", "card-meta", `Alert threshold: ${thresholdText(s)}`));

    const last = alertList.find((a) => a.sensor_id === s.sensor_id);
    const recent = last && Date.now() - Date.parse(last.timestamp) < 15 * 60000;
    card.append(last ? agoEl("div", recent ? "card-meta recent-alert" : "card-meta", last.timestamp, "Last alert: ")
                     : el("div", "card-meta", "Last alert: none recently"));
    return card;
  }));
}

// ---------- rendering: chart ----------

// Ticks aligned to the local clock (e.g. every 3 h at 00:00, 03:00 ... or daily at midnight).
function buildLocalTicks(axis) {
  const span = axis.max - axis.min;
  const stepMin = TICK_STEPS_MIN.find((m) => span / (m * 60000) <= 8) ?? 1440 * Math.ceil(span / (8 * DAY_MS));
  tickStepMs = stepMin * 60000;
  const offsetMs = new Date(axis.min).getTimezoneOffset() * 60000;
  const ticks = [];
  for (let t = Math.ceil((axis.min - offsetMs) / tickStepMs) * tickStepMs + offsetMs; t <= axis.max; t += tickStepMs) {
    ticks.push({ value: t });
  }
  axis.ticks = ticks;
}

function fmtTick(value) {
  const d = new Date(value);
  const date = d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  if (tickStepMs >= DAY_MS) return date;
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.getHours() === 0 && d.getMinutes() === 0 ? [time, date] : time;
}

function areaGradient(context) {
  const { chart: c } = context;
  if (!c.chartArea || !chartSensor) return "transparent";
  const rgb = SENSOR_RGB[chartSensor.sensor_type];
  const g = c.ctx.createLinearGradient(0, c.chartArea.top, 0, c.chartArea.bottom);
  g.addColorStop(0, rgba(rgb, 0.22));
  g.addColorStop(1, rgba(rgb, 0.01));
  return g;
}

function ensureChart() {
  if (chart) return chart;
  chart = new Chart($("chart"), {
    type: "line",
    data: {
      datasets: [
        {
          label: "reading", data: [], borderWidth: 2, tension: 0.3, fill: "start",
          backgroundColor: areaGradient,
          pointRadius: (ctx) => (ctx.raw && chartSensor && isBreached(chartSensor, ctx.raw.y) ? 3 : 0),
          pointHoverRadius: 5, pointBackgroundColor: "#dc2626", pointBorderWidth: 0,
        },
        {
          label: "threshold", data: [], borderColor: "#dc2626", borderWidth: 1.5, borderDash: [6, 4],
          pointRadius: 0, pointHoverRadius: 0, tension: 0, backgroundColor: "rgba(220, 38, 38, 0.07)",
        },
      ],
    },
    options: {
      animation: false,
      parsing: false,
      normalized: true,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      scales: {
        x: {
          type: "linear",
          afterBuildTicks: buildLocalTicks,
          ticks: { callback: fmtTick, autoSkip: false, maxRotation: 0, color: "#687386" },
          grid: { color: "#eef1f5" },
        },
        y: {
          grace: "5%",
          title: { display: true, text: "", color: "#687386" },
          ticks: { color: "#687386" },
          grid: { color: "#eef1f5" },
        },
      },
      plugins: {
        legend: { display: false },
        decimation: { enabled: true, algorithm: "lttb", samples: 800 },
        tooltip: {
          filter: (item) => item.datasetIndex === 0,
          callbacks: {
            title: (items) => new Date(items[0].parsed.x).toLocaleString(),
            label: (item) => ` ${chartSensor.sensor_name}: ${fmtNum(item.parsed.y)} ${chartSensor.unit}`,
            afterLabel: (item) => (isBreached(chartSensor, item.parsed.y) ? " Beyond alert threshold" : ""),
          },
        },
      },
    },
  });
  return chart;
}

function renderLegend(sensor) {
  const rgb = SENSOR_RGB[sensor.sensor_type];
  const item = (swatchClass, style, text) => {
    const span = el("span", "legend-item");
    const sw = el("span", `swatch ${swatchClass}`);
    if (style) sw.style.borderTopColor = style;
    span.append(sw, document.createTextNode(text));
    return span;
  };
  const zone = sensor.alert_rule.condition === ">" ? "above" : "below";
  $("chart-legend").replaceChildren(
    item("", rgba(rgb, 1), `${sensor.sensor_name} (${sensor.unit})`),
    item("dashed", "#dc2626", `Alert threshold ${thresholdText(sensor)}`),
    item("zone", null, `Alert zone (${zone} threshold)`),
    item("point", null, "Reading beyond threshold"),
  );
}

function renderChart(readings, range, sensor) {
  ensureChart();
  chartSensor = sensor;
  const rgb = SENSOR_RGB[sensor.sensor_type];
  const [line, threshold] = chart.data.datasets;
  line.data = readings.map((r) => ({ x: Date.parse(r.timestamp), y: r.value }));
  line.borderColor = rgba(rgb, 1);
  const startMs = range.start.getTime();
  const endMs = range.end.getTime();
  threshold.data = [{ x: startMs, y: sensor.alert_rule.threshold }, { x: endMs, y: sensor.alert_rule.threshold }];
  threshold.fill = sensor.alert_rule.condition === ">" ? "end" : "start";
  chart.options.scales.x.min = startMs;
  chart.options.scales.x.max = endMs;
  chart.options.scales.y.title.text = `${sensor.sensor_name} (${sensor.unit})`;
  chart.update("none");

  $("chart-title").textContent = `${sensor.sensor_name} history · ${sensor.sensor_id}`;
  $("chart-subtitle").textContent =
    `${rangeLabel()} · ${fmtInt(readings.length)} readings · unit ${sensor.unit} · alert when ${thresholdText(sensor)}`;
  renderLegend(sensor);

  const overlay = $("chart-overlay");
  overlay.hidden = readings.length > 0;
  overlay.textContent = "No readings in this time range";
}

function renderStats(stats, sensor) {
  const show = (id, v) => {
    const node = $(id);
    node.textContent = v === null ? "–" : `${fmtNum(v)} ${sensor.unit}`;
    node.classList.toggle("bad", v !== null && isBreached(sensor, v));
  };
  show("stat-min", stats.min);
  show("stat-max", stats.max);
  show("stat-avg", stats.avg);
  $("stat-count").textContent = fmtInt(stats.count);
}

// ---------- rendering: alerts and tables ----------

function renderAlerts(alertList, sensors) {
  const ul = $("alerts");
  if (alertList.length === 0) {
    ul.replaceChildren(el("li", "empty", "No alerts stored yet. Use Demo mode to trigger one."));
    return;
  }
  const byId = Object.fromEntries(sensors.map((s) => [s.sensor_id, s]));
  const seen = new Set();
  ul.replaceChildren(...alertList.map((a) => {
    const s = byId[a.sensor_id];
    // Only a sensor's newest alert can still be active, and only while its latest reading is breached.
    const active = !seen.has(a.sensor_id) && s && s.in_alert;
    seen.add(a.sensor_id);

    const li = el("li", active ? "alert-item is-active" : "alert-item");
    li.append(
      el("span", "alert-type", a.alert_type.replace(/_/g, " ")),
      el("span", "alert-value", `${fmtNum(a.value)} ${a.unit}`),
      el("span", "alert-sensor", `${a.sensor_name} sensor · ${a.sensor_id}`),
      agoEl("span", "alert-time", a.timestamp),
      el("span", "alert-threshold", `Threshold: ${s ? thresholdText(s) : "–"} · ${fmtDateTime(a.timestamp)}`),
      el("span", active ? "pill pill-active" : "pill pill-resolved", active ? "ACTIVE" : "RESOLVED"),
    );
    return li;
  }));
}

function renderHourly(hourly, sensor) {
  const cls = (v) => (isBreached(sensor, v) ? "num bad" : "num");
  fillRows("hourly", hourly.slice().reverse(),
    (h) => [[fmtHourBucket(h.hour)], [fmtNum(h.min), cls(h.min)], [fmtNum(h.max), cls(h.max)],
            [fmtNum(h.avg), "num"], [h.count, "num"]],
    "No readings in this range");
}

function renderRaw(raw, sensor) {
  fillRows("raw", raw.slice().reverse(), (r) => {
    const bad = isBreached(sensor, r.value);
    return [[fmtDateTime(r.timestamp)], [r.id, "num"], [r.sensor_id], [r.sensor_type],
            [fmtNum(r.value), bad ? "num bad" : "num"], [r.unit], [bad ? "ALERT" : "NORMAL", bad ? "bad" : "ok-text"]];
  }, "No readings in this range");
}

// ---------- refresh loop ----------

async function loadAll() {
  const [sensors, summary] = await Promise.all([getJSON("/api/sensors"), getJSON("/api/summary")]);
  if (!state.sensorId) state.sensorId = sensors[0].sensor_id;
  renderSensorSelect(sensors);

  const sensor = sensors.find((s) => s.sensor_id === state.sensorId);
  const range = currentRange();
  const q = rangeQuery(range);
  const [readings, stats, hourly, alertList, raw] = await Promise.all([
    getJSON(`/api/readings?${q}&limit=${CHART_LIMIT}`),
    getJSON(`/api/stats?${q}`),
    getJSON(`/api/stats/hourly?${q}`),
    getJSON("/api/alerts?limit=20"),
    getJSON(`/api/readings?${q}&limit=20`),
  ]);

  renderSummary(summary);
  renderCards(sensors, alertList);
  renderChart(readings, range, sensor);
  renderStats(stats, sensor);
  renderAlerts(alertList, sensors);
  renderHourly(hourly, sensor);
  renderRaw(raw, sensor);
}

// Runs one refresh; never overlaps, and schedules the next one only after this one finishes.
async function refresh() {
  if (running) {
    rerun = true;
    return;
  }
  running = true;
  clearTimeout(timer);
  try {
    await loadAll();
    setOnline(true);
    document.body.classList.remove("is-loading");
    const time = new Date().toLocaleTimeString();
    $("st-refresh").textContent = state.paused ? `Paused · ${time}` : time;
  } catch (err) {
    setOnline(false, err.message);
    if (document.body.classList.contains("is-loading")) {
      $("chart-overlay").textContent = "Cannot load data from the API";
    }
  } finally {
    running = false;
  }
  if (rerun) {
    rerun = false;
    refresh();
    return;
  }
  if (!state.paused) timer = setTimeout(refresh, REFRESH_MS);
}

// ---------- controls ----------

function selectSensor(sensorId) {
  state.sensorId = sensorId;
  $("sensor").value = sensorId;
  refresh();
}

function setActivePreset(minutes) {
  document.querySelectorAll(".presets button").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.minutes) === minutes);
  });
}

async function triggerDemo(button) {
  const sensorId = button.dataset.sensor;
  const value = Number(button.dataset.value);
  const result = $("demo-result");
  button.disabled = true;
  try {
    const res = await postJSON("/api/readings", { sensor_id: sensorId, value });
    if (res.alert) {
      result.className = "demo-result ok";
      const where = document.body.dataset.mode === "demo" ? "kept for this browser (demo mode)" : "stored in SQLite";
      result.textContent = `Reading #${res.reading.id} (${value}) ${where} → ${res.alert.alert_type} alert #${res.alert.id} created.`;
    } else {
      result.className = "demo-result info";
      result.textContent = `Reading #${res.reading.id} stored. No new alert: ${sensorId} was already beyond its threshold ` +
        "(alerts are raised only when a sensor crosses from normal to abnormal).";
    }
    selectSensor(sensorId);
  } catch (err) {
    result.className = "demo-result bad";
    result.textContent = `Request failed: ${err.message}`;
  } finally {
    button.disabled = false;
  }
}

function initControls() {
  $("sensor").addEventListener("change", (e) => selectSensor(e.target.value));

  document.querySelectorAll(".presets button").forEach((b) => {
    b.addEventListener("click", () => {
      state.presetMinutes = Number(b.dataset.minutes);
      setActivePreset(state.presetMinutes);
      refresh();
    });
  });

  $("apply").addEventListener("click", () => {
    // datetime-local values have no time zone, so the browser reads them as local time.
    const start = new Date($("from").value);
    const end = new Date($("to").value);
    if (isNaN(start) || isNaN(end) || start >= end) {
      $("chart-subtitle").textContent = "Custom range: choose a From time earlier than the To time.";
      return;
    }
    state.presetMinutes = null;
    state.customStart = start;
    state.customEnd = end;
    setActivePreset(null);
    refresh();
  });

  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause").textContent = state.paused ? "Resume refresh" : "Pause refresh";
    $("pause").classList.toggle("active", state.paused);
    if (state.paused) {
      clearTimeout(timer);
      $("st-refresh").textContent = "Paused";
    } else {
      refresh();
    }
  });

  document.querySelectorAll(".btn-demo").forEach((b) => b.addEventListener("click", () => triggerDemo(b)));

  const now = new Date();
  $("from").value = toLocalInput(new Date(now.getTime() - 3600 * 1000));
  $("to").value = toLocalInput(now);
}

initControls();
tickClock();
setInterval(tickClock, 1000);
refresh();
