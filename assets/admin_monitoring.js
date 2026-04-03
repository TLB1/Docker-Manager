"use strict";

// ------------------------------------------------------------------ //
// Bootstrap                                                            //
// ------------------------------------------------------------------ //

const _meta       = document.getElementById("monitor-meta");
const CSRF        = _meta.dataset.csrf;
const METRICS_URL = _meta.dataset.metricsUrl;
const HISTORY_URL = _meta.dataset.historyUrl;

const METRICS_INTERVAL_MS = 10_000;   // 10 s  – node cards + container table
const HISTORY_INTERVAL_MS = 30_000;   // 30 s  – Chart.js graphs

/** Live Chart.js instances keyed by their canvas id. */
const _charts = {};

// ------------------------------------------------------------------ //
// Utility                                                              //
// ------------------------------------------------------------------ //

/** Escape a value for safe insertion into innerHTML. */
function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Turn an IP/hostname into a string safe for use as a DOM id. */
function safeId(str) {
  return String(str).replace(/[^a-zA-Z0-9]/g, "_");
}

function setStatus(ok) {
  const badge = document.getElementById("status-badge");
  badge.className = ok ? "badge bg-success" : "badge bg-danger";
  badge.textContent = ok ? "Live" : "Error";
}

function setLastUpdated() {
  document.getElementById("last-updated").textContent =
    "Updated " + new Date().toLocaleTimeString();
}

// ------------------------------------------------------------------ //
// Network                                                              //
// ------------------------------------------------------------------ //

async function apiFetch(url) {
  const resp = await fetch(url, {
    credentials: "same-origin",
    headers: { "CSRF-Token": CSRF },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// ------------------------------------------------------------------ //
// Chart.js – historical graphs                                         //
// ------------------------------------------------------------------ //

/**
 * Create or update a Chart.js line chart.
 *
 * If the canvas has already been used, we update the existing instance in
 * place (no animation) to avoid flickering.
 */
function upsertChart(canvasId, labels, datasets, yUnit) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  if (_charts[canvasId]) {
    const ch = _charts[canvasId];
    ch.data.labels   = labels;
    ch.data.datasets = datasets;
    ch.update("none");
    return;
  }

  _charts[canvasId] = new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive:  true,
      animation:   false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend:  { display: datasets.length > 1, position: "top", labels: { boxWidth: 10, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y}${yUnit ? " " + yUnit : ""}`,
          },
        },
      },
      scales: {
        x: {
          ticks:   { maxTicksLimit: 6, font: { size: 10 } },
          grid:    { display: false },
        },
        y: {
          beginAtZero: true,
          ticks:       { font: { size: 10 } },
          grid:        { color: "rgba(0,0,0,0.05)" },
        },
      },
    },
  });
}

function destroyChartsFor(prefix) {
  Object.keys(_charts).forEach(key => {
    if (key.startsWith(prefix)) {
      _charts[key].destroy();
      delete _charts[key];
    }
  });
}

function renderCharts(data) {
  const wrap  = document.getElementById("charts-container");
  const nodes = data.nodes || {};

  if (Object.keys(nodes).length === 0) {
    wrap.innerHTML = '<div class="col-12 text-center text-muted py-3"><small>No history yet.</small></div>';
    return;
  }

  // Build HTML once (we only inject new HTML if nodes change)
  const existingIds = new Set(
    Array.from(wrap.querySelectorAll("[data-node-chart]")).map(el => el.dataset.nodeChart)
  );
  const incomingIds = new Set(Object.keys(nodes));

  const needsRebuild =
    [...existingIds].some(id => !incomingIds.has(id)) ||
    [...incomingIds].some(id => !existingIds.has(id));

  if (needsRebuild) {
    // Destroy stale Chart.js instances before wiping the DOM
    destroyChartsFor("chart-mem-");
    destroyChartsFor("chart-cpu-");
    destroyChartsFor("chart-ctr-");
    const nodeCount = Object.keys(nodes).length;
    const colisize  = nodeCount % 3 === 0 || nodeCount > 4 ? 4
                    : nodeCount % 2 === 0 ? 6
                    : nodeCount === 1     ? 12
                    : 6;
    wrap.innerHTML = Object.entries(nodes).map(([addr, d]) => {
      const sid = safeId(addr);
      return `
        <div class="col-md-${colisize} mb-4" data-node-chart="${esc(addr)}">
          <div class="card shadow-sm">
            <div class="card-header bg-white d-flex align-items-center justify-content-between">
              <strong>${esc(addr)}</strong>
              <small class="text-muted">${esc(d.name || "")}</small>
            </div>
            <div class="card-body pb-2">
              <p class="small text-muted mb-1">Memory Usage (MB)</p>
              <canvas id="chart-mem-${sid}" height="120"></canvas>
              <p class="small text-muted mt-3 mb-1">Total CPU Usage (%)</p>
              <canvas id="chart-cpu-${sid}" height="80"></canvas>
              <p class="small text-muted mt-3 mb-1">Active Containers</p>
              <canvas id="chart-ctr-${sid}" height="70"></canvas>
            </div>
          </div>
        </div>`;
    }).join("");
  }

  // Create or update charts
  Object.entries(nodes).forEach(([addr, d]) => {
    const sid = safeId(addr);

    upsertChart(
      `chart-mem-${sid}`,
      d.labels,
      [
        {
          label:           "Used",
          data:            d.used_mem_mb,
          borderColor:     "#dc3545",
          backgroundColor: "rgba(220,53,69,0.1)",
          fill:            true,
          tension:         0.3,
          pointRadius:     2,
        },
        {
          label:           "Free",
          data:            d.free_mem_mb,
          borderColor:     "#28a745",
          backgroundColor: "rgba(40,167,69,0.1)",
          fill:            true,
          tension:         0.3,
          pointRadius:     2,
        },
      ],
      "MB"
    );

    upsertChart(
      `chart-cpu-${sid}`,
      d.labels,
      [
        {
          label:           "CPU %",
          data:            d.cpu_total_percent,
          borderColor:     "#fd7e14",
          backgroundColor: "rgba(253,126,20,0.1)",
          fill:            true,
          tension:         0.3,
          pointRadius:     2,
        },
      ],
      "%"
    );

    upsertChart(
      `chart-ctr-${sid}`,
      d.labels,
      [
        {
          label:           "Running",
          data:            d.running_count,
          borderColor:     "#0d6efd",
          backgroundColor: "rgba(13,110,253,0.1)",
          fill:            true,
          tension:         0.3,
          stepped:         true,
          pointRadius:     2,
        },
      ],
      ""
    );
  });
}

// ------------------------------------------------------------------ //
// Container stats table                                                //
// ------------------------------------------------------------------ //

let _containerData = [];

function renderContainerTable(containers) {
  _containerData = containers || [];
  updateContainerTable();
}

function updateContainerTable() {
  const search = document.getElementById("containerSearch").value.trim().toLowerCase();
  const sort   = document.getElementById("containerSort").value;

  let rows = _containerData.filter(c =>
    !search ||
    c.challenge.toLowerCase().includes(search) ||
    c.team.toLowerCase().includes(search)      ||
    c.node.toLowerCase().includes(search)      ||
    c.image.toLowerCase().includes(search)
  );

  rows.sort((a, b) => {
    switch (sort) {
      case "cpu":       return b.cpu_percent  - a.cpu_percent;
      case "mem":       return b.mem_usage_mb - a.mem_usage_mb;
      case "status":    return a.status.localeCompare(b.status);
      case "challenge": return a.challenge.localeCompare(b.challenge);
      default:          return 0;
    }
  });

  const tbody = document.getElementById("container-tbody");

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-3">No containers.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map(c => {
    const isRunning  = c.status === "running";
    const dotColor   = isRunning ? "#28a745" : "#6c757d";
    const cpuClass   = c.cpu_percent > 80 ? "text-danger fw-bold"
                     : c.cpu_percent > 50 ? "text-warning fw-bold" : "";
    const memPct     = c.mem_limit_mb > 0
      ? Math.round((c.mem_usage_mb / c.mem_limit_mb) * 100) : 0;
    const memBarCls  = memPct > 85 ? "bg-danger" : memPct > 65 ? "bg-warning" : "bg-primary";

    return `
      <tr>
        <td>
          <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${dotColor};"></span>
        </td>
        <td><strong>${esc(c.challenge)}</strong></td>
        <td>${esc(c.team)}</td>
        <td><small class="text-muted">${esc(c.node)}</small></td>
        <td><small class="text-muted">${esc(c.image)}</small></td>
        <td class="${cpuClass}">${c.cpu_percent.toFixed(1)}%</td>
        <td>
          <div class="progress mb-1" style="height:5px;">
            <div class="progress-bar ${memBarCls}" style="width:${memPct}%;"></div>
          </div>
          <small class="text-muted">${c.mem_usage_mb.toFixed(0)} / ${c.mem_limit_mb.toFixed(0)} MB</small>
        </td>
        <td>
          <a href="/admin/docker_manager/nodes"
             class="btn btn-sm btn-outline-secondary"
             style="font-size:0.75rem;padding:2px 8px;">Manage</a>
        </td>
      </tr>`;
  }).join("");
}

// ------------------------------------------------------------------ //
// Activity log                                                         //
// ------------------------------------------------------------------ //

const _seenTimestamps = new Set();

function renderEvents(events) {
  if (!events || events.length === 0) return;

  const newEvents = events.filter(e => !_seenTimestamps.has(e.timestamp));
  if (newEvents.length === 0) return;

  const log  = document.getElementById("activity-log");
  const ph   = document.getElementById("log-placeholder");
  if (ph) ph.remove();

  const levelClass = { info: "text-secondary", warning: "text-warning", error: "text-danger" };

  // Insert newest events at the top
  newEvents.reverse().forEach(e => {
    _seenTimestamps.add(e.timestamp);
    const ts  = new Date(e.timestamp * 1000).toLocaleTimeString();
    const cls = levelClass[e.level] || "text-muted";
    const row = document.createElement("div");
    row.className = `py-1 border-bottom ${cls}`;
    row.innerHTML = `<span class="me-2" style="opacity:0.45;">${ts}</span>${esc(e.message)}`;
    log.insertBefore(row, log.firstChild);
  });
}

document.getElementById("clear-log-btn").addEventListener("click", () => {
  _seenTimestamps.clear();
  const log = document.getElementById("activity-log");
  log.innerHTML = '<div class="text-muted text-center py-2" id="log-placeholder">Log cleared.</div>';
});

// ------------------------------------------------------------------ //
// Poll loops                                                           //
// ------------------------------------------------------------------ //

async function pollMetrics() {
  try {
    const data = await apiFetch(METRICS_URL);
    renderContainerTable(data.containers);
    renderEvents(data.events || []);
    setStatus(true);
    setLastUpdated();
  } catch (err) {
    console.error("[Monitoring] metrics poll failed:", err);
    setStatus(false);
  }
}

async function pollHistory() {
  try {
    const data = await apiFetch(HISTORY_URL);
    renderCharts(data);
  } catch (err) {
    console.error("[Monitoring] history poll failed:", err);
  }
}

// ------------------------------------------------------------------ //
// Wire up search/sort listeners then kick off initial fetches          //
// ------------------------------------------------------------------ //

document.getElementById("containerSearch").addEventListener("input",  updateContainerTable);
document.getElementById("containerSort").addEventListener("change",   updateContainerTable);

// Initial load
pollMetrics();
pollHistory();

// Recurring polls
setInterval(pollMetrics, METRICS_INTERVAL_MS);
setInterval(pollHistory, HISTORY_INTERVAL_MS);
