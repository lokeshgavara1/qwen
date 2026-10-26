/**
 * CUTM AI Gateway - API Dashboard Controller
 * Dynamic real-time telemetry, interactive dual-axis chart, quota management & audit logs
 */

const STATE = {
  stats: null,
  keys: [],
  workers: [],
  logs: [],
  refreshIntervalMs: 5000,
  timerId: null,
  searchQuery: "",
  logFilter: "all",
  logSearch: "",
};

// --- Initialization ---
document.addEventListener("DOMContentLoaded", () => {
  initEventListeners();
  loadAllData();
  setupAutoRefresh();
});

function initEventListeners() {
  // Auto refresh select
  const refreshSelect = document.getElementById("refreshSelect");
  if (refreshSelect) {
    refreshSelect.addEventListener("change", (e) => {
      const val = parseInt(e.target.value, 10);
      STATE.refreshIntervalMs = val;
      setupAutoRefresh();
    });
  }

  // Key search filter
  const keySearchInput = document.getElementById("keySearchInput");
  if (keySearchInput) {
    keySearchInput.addEventListener("input", (e) => {
      STATE.searchQuery = e.target.value.toLowerCase().trim();
      renderKeysTable();
    });
  }

  // Audit log search
  const logSearchInput = document.getElementById("logSearchInput");
  if (logSearchInput) {
    logSearchInput.addEventListener("input", (e) => {
      STATE.logSearch = e.target.value.toLowerCase().trim();
      renderLogsTable();
    });
  }

  // Log filter buttons
  document.querySelectorAll(".log-filter-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      document.querySelectorAll(".log-filter-btn").forEach((b) => b.classList.remove("active"));
      e.target.classList.add("active");
      STATE.logFilter = e.target.getAttribute("data-filter") || "all";
      renderLogsTable();
    });
  });

  // Modal events
  document.getElementById("btnNewKey")?.addEventListener("click", openCreateKeyModal);
  document.getElementById("closeNewKeyModal")?.addEventListener("click", closeCreateKeyModal);
  document.getElementById("cancelNewKeyBtn")?.addEventListener("click", closeCreateKeyModal);
  document.getElementById("formCreateKey")?.addEventListener("submit", handleCreateKeySubmit);

  // Edit Quota modal
  document.getElementById("closeQuotaModal")?.addEventListener("click", closeQuotaModal);
  document.getElementById("cancelQuotaBtn")?.addEventListener("click", closeQuotaModal);
  document.getElementById("formEditQuota")?.addEventListener("submit", handleEditQuotaSubmit);

  // Export CSV
  document.getElementById("btnExportCsv")?.addEventListener("click", exportAuditLogsCsv);

  // Manual Refresh Button
  document.getElementById("btnManualRefresh")?.addEventListener("click", () => {
    loadAllData();
  });
}

function setupAutoRefresh() {
  if (STATE.timerId) clearInterval(STATE.timerId);
  if (STATE.refreshIntervalMs > 0) {
    STATE.timerId = setInterval(loadAllData, STATE.refreshIntervalMs);
  }
}

// --- Data Fetching ---
async function loadAllData() {
  try {
    await Promise.all([
      fetchStats(),
      fetchKeys(),
      fetchWorkers(),
      fetchLogs(),
    ]);
  } catch (err) {
    console.error("Dashboard refresh error:", err);
  }
}

async function fetchStats() {
  try {
    const res = await fetch("/api/dashboard/stats");
    if (!res.ok) return;
    const data = await res.json();
    STATE.stats = data;
    renderKPIs(data.kpis);
    renderTrafficChart(data.traffic || []);
    renderModelBreakdown(data.models || []);
  } catch (e) {
    console.warn("fetchStats failed", e);
  }
}

async function fetchKeys() {
  try {
    const res = await fetch("/api/dashboard/keys");
    if (!res.ok) return;
    const data = await res.json();
    STATE.keys = data.keys || [];
    renderKeysTable();
  } catch (e) {
    console.warn("fetchKeys failed", e);
  }
}

async function fetchWorkers() {
  try {
    const res = await fetch("/api/dashboard/workers");
    if (!res.ok) return;
    const data = await res.json();
    STATE.workers = data.workers || [];
    renderWorkersGrid(STATE.workers);
  } catch (e) {
    console.warn("fetchWorkers failed", e);
  }
}

async function fetchLogs() {
  try {
    const res = await fetch("/api/dashboard/logs?limit=80");
    if (!res.ok) return;
    const data = await res.json();
    STATE.logs = data.logs || [];
    renderLogsTable();
  } catch (e) {
    console.warn("fetchLogs failed", e);
  }
}

// --- Render KPI Cards ---
function renderKPIs(kpis) {
  if (!kpis) return;

  const totalEl = document.getElementById("kpiTotalReqs");
  const todayTrendEl = document.getElementById("kpiTrendPill");
  const activeKeysEl = document.getElementById("kpiActiveKeys");
  const latencyEl = document.getElementById("kpiAvgLatency");
  const errorRateEl = document.getElementById("kpiErrorRate");
  const tokensEl = document.getElementById("kpiTotalTokens");
  const tpsEl = document.getElementById("kpiTokensPerSec");

  if (totalEl) totalEl.textContent = Number(kpis.total_requests).toLocaleString();
  if (todayTrendEl) {
    const trend = kpis.trend_pct || 0;
    todayTrendEl.textContent = `${trend >= 0 ? "+" : ""}${trend}% today`;
    todayTrendEl.className = `trend-pill ${trend >= 0 ? "trend-up" : "trend-down"}`;
  }
  if (activeKeysEl) activeKeysEl.textContent = kpis.active_keys;
  if (latencyEl) latencyEl.textContent = `${kpis.avg_latency_ms} ms`;
  if (errorRateEl) errorRateEl.textContent = `${kpis.error_rate_pct}%`;
  if (tokensEl) {
    const t = kpis.total_tokens || 0;
    tokensEl.textContent = t > 1000000 ? `${(t / 1000000).toFixed(2)}M` : `${(t / 1000).toFixed(1)}k`;
  }
  if (tpsEl) tpsEl.textContent = `${kpis.tokens_per_sec} tok/s`;
}

// --- Render Traffic Volume & Latency Dual Chart ---
function renderTrafficChart(traffic) {
  const canvas = document.getElementById("trafficCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  // Fix resolution for high DPI displays
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);

  const width = rect.width;
  const height = rect.height;
  ctx.clearRect(0, 0, width, height);

  if (!traffic || traffic.length === 0) {
    ctx.fillStyle = "#64748B";
    ctx.font = "13px Inter";
    ctx.textAlign = "center";
    ctx.fillText("Awaiting traffic telemetry...", width / 2, height / 2);
    return;
  }

  const padding = { top: 20, right: 40, bottom: 35, left: 45 };
  const chartW = width - padding.left - padding.right;
  const chartH = height - padding.top - padding.bottom;

  // Find max values
  const maxReq = Math.max(...traffic.map((d) => d.total), 10);
  const maxLat = Math.max(...traffic.map((d) => d.latency), 500);

  // Draw Grid Lines
  ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = padding.top + (chartH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();

    // Left Y label (Requests)
    const reqVal = Math.round(maxReq - (maxReq / 4) * i);
    ctx.fillStyle = "#64748B";
    ctx.font = "10px Inter";
    ctx.textAlign = "right";
    ctx.fillText(reqVal, padding.left - 8, y + 3);
  }

  // Draw Request Volume Bars
  const barWidth = Math.max(8, Math.min(24, (chartW / traffic.length) * 0.65));
  traffic.forEach((d, i) => {
    const x = padding.left + (chartW / traffic.length) * (i + 0.5);
    const barH = (d.total / maxReq) * chartH;
    const y = padding.top + chartH - barH;

    // Gradient fill for bars
    const grad = ctx.createLinearGradient(0, y, 0, padding.top + chartH);
    grad.addColorStop(0, "rgba(0, 242, 254, 0.85)");
    grad.addColorStop(1, "rgba(79, 172, 254, 0.2)");

    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.roundRect(x - barWidth / 2, y, barWidth, barH, [4, 4, 0, 0]);
    ctx.fill();

    // X-axis label (every 2 or 3 bars)
    if (i % 3 === 0 || i === traffic.length - 1) {
      ctx.fillStyle = "#94A3B8";
      ctx.font = "10px Inter";
      ctx.textAlign = "center";
      ctx.fillText(d.time, x, height - 10);
    }
  });

  // Draw Latency Line Overlay
  ctx.beginPath();
  ctx.strokeStyle = "#8B5CF6";
  ctx.lineWidth = 2.5;
  ctx.shadowColor = "rgba(139, 92, 246, 0.5)";
  ctx.shadowBlur = 8;

  traffic.forEach((d, i) => {
    const x = padding.left + (chartW / traffic.length) * (i + 0.5);
    const y = padding.top + chartH - (d.latency / maxLat) * chartH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.shadowBlur = 0; // reset shadow

  // Latency data dots
  traffic.forEach((d, i) => {
    const x = padding.left + (chartW / traffic.length) * (i + 0.5);
    const y = padding.top + chartH - (d.latency / maxLat) * chartH;
    ctx.fillStyle = "#C084FC";
    ctx.beginPath();
    ctx.arc(x, y, 3.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#080B11";
    ctx.lineWidth = 1.5;
    ctx.stroke();
  });
}

// --- Render Model Breakdown List & Donut ---
const MODEL_COLORS = {
  "mistral:7b": "#00F2FE",
  "FLUX.1-schnell": "#8B5CF6",
  "qwen2.5vl:7b": "#10B981",
  "whisper-large-v3": "#F59E0B",
  "qwen2.5-coder:32b": "#EC4899",
  "other": "#64748B",
};

function renderModelBreakdown(models) {
  const container = document.getElementById("modelListContainer");
  if (!container) return;

  if (!models || models.length === 0) {
    container.innerHTML = `<div style="color: var(--text-muted); font-size: 0.85rem; padding: 10px;">No models recorded yet</div>`;
    return;
  }

  container.innerHTML = models
    .map((m) => {
      const color = MODEL_COLORS[m.model] || "#4FACFE";
      return `
      <div class="model-item">
        <div class="model-info">
          <span class="model-color-dot" style="background-color: ${color}; box-shadow: 0 0 8px ${color}"></span>
          <span class="model-name">${escapeHtml(m.model)}</span>
        </div>
        <div class="model-stats">
          <span class="model-count">${Number(m.count).toLocaleString()} reqs</span>
          <span class="model-pct">${m.percentage}%</span>
        </div>
      </div>
    `;
    })
    .join("");
}

// --- Render "Who is Using How Much" Key Analytics Table ---
function renderKeysTable() {
  const tbody = document.getElementById("keysTableBody");
  if (!tbody) return;

  let list = STATE.keys || [];
  if (STATE.searchQuery) {
    list = list.filter(
      (k) =>
        k.name.toLowerCase().includes(STATE.searchQuery) ||
        k.masked_key.toLowerCase().includes(STATE.searchQuery) ||
        k.role.toLowerCase().includes(STATE.searchQuery)
    );
  }

  if (list.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 32px;">
          No matching API keys found.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = list
    .map((k) => {
      // Quota color logic
      const pct = k.quota_percent || 0;
      let quotaClass = "quota-green";
      let pctColor = "var(--accent-green)";
      if (pct >= 90) {
        quotaClass = "quota-red";
        pctColor = "var(--accent-red)";
      } else if (pct >= 70) {
        quotaClass = "quota-yellow";
        pctColor = "var(--accent-yellow)";
      }

      const activeBadge = k.active
        ? `<span class="status-pill status-active">Active</span>`
        : `<span class="status-pill status-disabled">Revoked</span>`;

      const lastActiveFormatted = k.last_active ? formatRelativeTime(k.last_active) : "Never";

      return `
      <tr>
        <td>
          <div class="client-cell">
            <div class="client-avatar">${escapeHtml(k.name.charAt(0).toUpperCase())}</div>
            <div>
              <div class="client-title">${escapeHtml(k.name)}</div>
              <div class="client-desc">Role: ${escapeHtml(k.role)} &bull; ${escapeHtml(k.top_endpoint)}</div>
            </div>
          </div>
        </td>
        <td>
          <span class="key-badge" title="Click to copy full key snippet" onclick="copyToClipboard('${escapeHtml(k.key_id)}')">
            ${escapeHtml(k.masked_key)}
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
          </span>
        </td>
        <td>
          <div style="font-weight: 700; color: #FFF;">${Number(k.total_requests).toLocaleString()} reqs</div>
          <div style="font-size: 0.74rem; color: var(--text-muted);">${formatTokens(k.total_tokens)} tokens</div>
        </td>
        <td>
          <div class="quota-box">
            <div class="quota-header">
              <span style="color: var(--text-secondary);">${Number(k.today_requests).toLocaleString()} / ${Number(k.daily_limit).toLocaleString()}</span>
              <span class="quota-percent" style="color: ${pctColor}">${pct}%</span>
            </div>
            <div class="progress-track">
              <div class="progress-fill ${quotaClass}" style="width: ${Math.min(100, pct)}%"></div>
            </div>
          </div>
        </td>
        <td>
          <div style="font-size: 0.82rem; color: var(--text-secondary);">${lastActiveFormatted}</div>
        </td>
        <td>${activeBadge}</td>
        <td>
          <div style="display: flex; gap: 8px;">
            <button class="btn" style="padding: 4px 10px; font-size: 0.76rem;" onclick="openEditQuotaModal('${escapeHtml(k.key_id)}', ${k.daily_limit})">
              Quota
            </button>
            <button class="btn" style="padding: 4px 10px; font-size: 0.76rem; color: ${k.active ? 'var(--accent-red)' : 'var(--accent-green)'}" onclick="toggleKeyStatus('${escapeHtml(k.key_id)}', ${!k.active})">
              ${k.active ? "Revoke" : "Enable"}
            </button>
          </div>
        </td>
      </tr>
    `;
    })
    .join("");
}

// --- Render Cluster Worker Nodes Grid ---
function renderWorkersGrid(workers) {
  const container = document.getElementById("workersGridContainer");
  if (!container) return;

  if (!workers || workers.length === 0) {
    container.innerHTML = `<div style="color: var(--text-muted); font-size: 0.85rem;">No worker node telemetry available</div>`;
    return;
  }

  container.innerHTML = workers
    .map((w) => {
      const isOnline = w.online;
      const statusPill = isOnline
        ? `<span class="status-pill status-active"><span class="pulse-dot" style="width:5px;height:5px;"></span> Online</span>`
        : `<span class="status-pill status-disabled">Offline</span>`;

      return `
      <div class="worker-node-card">
        <div class="worker-header">
          <div class="worker-role">${escapeHtml(w.role)}</div>
          ${statusPill}
        </div>
        <div class="worker-meta">
          <div><strong>URL:</strong> ${escapeHtml(w.url)}</div>
          <div><strong>Queue:</strong> ${w.queue} reqs &bull; <strong>Latency:</strong> ${w.latency_ms || 0}ms</div>
          <div><strong>Models:</strong> ${(w.models || []).slice(0, 2).join(", ")}</div>
        </div>
      </div>
    `;
    })
    .join("");
}

// --- Render Live Audit Logs Table ---
function renderLogsTable() {
  const tbody = document.getElementById("logsTableBody");
  if (!tbody) return;

  let list = STATE.logs || [];

  // Filter by status (2xx, 4xx, 5xx)
  if (STATE.logFilter !== "all") {
    list = list.filter((l) => {
      if (STATE.logFilter === "2xx") return l.status_code >= 200 && l.status_code < 300;
      if (STATE.logFilter === "4xx") return l.status_code >= 400 && l.status_code < 500;
      if (STATE.logFilter === "5xx") return l.status_code >= 500;
      return true;
    });
  }

  // Filter by search string
  if (STATE.logSearch) {
    list = list.filter(
      (l) =>
        (l.endpoint && l.endpoint.toLowerCase().includes(STATE.logSearch)) ||
        (l.api_key_name && l.api_key_name.toLowerCase().includes(STATE.logSearch)) ||
        (l.client_ip && l.client_ip.toLowerCase().includes(STATE.logSearch)) ||
        (l.model && l.model.toLowerCase().includes(STATE.logSearch))
    );
  }

  if (list.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 24px;">
          No matching request logs.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = list
    .slice(0, 50)
    .map((log) => {
      const code = log.status_code;
      let statusClass = "status-2xx";
      if (code >= 500) statusClass = "status-5xx";
      else if (code >= 400) statusClass = "status-4xx";

      const time = log.timestamp ? log.timestamp.split("T")[1]?.slice(0, 8) : "";

      return `
      <tr>
        <td style="font-family: monospace; color: var(--text-muted); font-size: 0.78rem;">${time}</td>
        <td><span class="status-pill ${statusClass}">${code}</span></td>
        <td><span style="font-weight: 600; color: var(--accent-cyan);">${escapeHtml(log.api_key_name || "Public")}</span></td>
        <td style="font-family: monospace; font-size: 0.8rem;">${escapeHtml(log.endpoint)}</td>
        <td><span style="color: var(--text-secondary); font-size: 0.78rem;">${escapeHtml(log.model || "-")}</span></td>
        <td style="font-family: monospace; font-size: 0.78rem; color: var(--text-muted);">${escapeHtml(log.client_ip || "-")}</td>
        <td style="font-variant-numeric: tabular-nums; font-weight: 600;">${log.latency_ms || 0} ms</td>
      </tr>
    `;
    })
    .join("");
}

// --- Key Management Actions ---
function openCreateKeyModal() {
  document.getElementById("modalCreateKey")?.classList.add("open");
}

function closeCreateKeyModal() {
  document.getElementById("modalCreateKey")?.classList.remove("open");
}

async function handleCreateKeySubmit(e) {
  e.preventDefault();
  const name = document.getElementById("newKeyName")?.value.trim();
  const role = document.getElementById("newKeyRole")?.value || "user";
  const quota = parseInt(document.getElementById("newKeyQuota")?.value || "10000", 10);
  const expiryDays = parseInt(document.getElementById("newKeyExpiry")?.value || "365", 10);

  if (!name) return alert("Key name is required");

  try {
    const res = await fetch("/api/dashboard/keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, role, daily_limit: quota, expiry_days: expiryDays }),
    });

    if (!res.ok) {
      const err = await res.json();
      return alert("Failed: " + (err.error || "Could not create key"));
    }

    const data = await res.json();
    closeCreateKeyModal();
    alert(`API Key Generated Successfully!\n\nToken: ${data.key}\n\nMake sure to copy it now as full token is masked later.`);
    loadAllData();
  } catch (err) {
    alert("Error creating key: " + err.message);
  }
}

let activeEditKeyId = null;
function openEditQuotaModal(keyId, currentLimit) {
  activeEditKeyId = keyId;
  const input = document.getElementById("editQuotaInput");
  if (input) input.value = currentLimit || 10000;
  document.getElementById("modalEditQuota")?.classList.add("open");
}

function closeQuotaModal() {
  activeEditKeyId = null;
  document.getElementById("modalEditQuota")?.classList.remove("open");
}

async function handleEditQuotaSubmit(e) {
  e.preventDefault();
  if (!activeEditKeyId) return;
  const limit = parseInt(document.getElementById("editQuotaInput")?.value || "10000", 10);

  try {
    const res = await fetch(`/api/dashboard/keys/${encodeURIComponent(activeEditKeyId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ daily_limit: limit }),
    });

    if (!res.ok) {
      return alert("Failed to update quota");
    }

    closeQuotaModal();
    loadAllData();
  } catch (err) {
    alert("Error updating quota: " + err.message);
  }
}

async function toggleKeyStatus(keyId, newStatus) {
  const actionName = newStatus ? "activate" : "revoke";
  if (!confirm(`Are you sure you want to ${actionName} key '${keyId.slice(0, 10)}...'?`)) return;

  try {
    const res = await fetch(`/api/dashboard/keys/${encodeURIComponent(keyId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: newStatus }),
    });

    if (!res.ok) return alert("Failed to update key status");
    loadAllData();
  } catch (err) {
    alert("Error: " + err.message);
  }
}

// --- CSV Export ---
function exportAuditLogsCsv() {
  if (!STATE.logs || STATE.logs.length === 0) {
    return alert("No audit logs available to export.");
  }

  const headers = ["ID", "Timestamp", "API Key Name", "Client IP", "Endpoint", "Model", "Status Code", "Latency (ms)", "Total Tokens"];
  const rows = STATE.logs.map((l) => [
    l.id,
    `"${l.timestamp}"`,
    `"${l.api_key_name || ''}"`,
    `"${l.client_ip || ''}"`,
    `"${l.endpoint || ''}"`,
    `"${l.model || ''}"`,
    l.status_code,
    l.latency_ms,
    l.total_tokens,
  ]);

  const csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows.map((e) => e.join(","))].join("\n");
  const encodedUri = encodeURI(csvContent);
  const link = document.createElement("a");
  link.setAttribute("href", encodedUri);
  link.setAttribute("download", `cutm_api_audit_${new Date().toISOString().slice(0, 10)}.csv`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

// --- Helpers ---
function formatTokens(t) {
  if (!t) return "0";
  if (t >= 1000000) return (t / 1000000).toFixed(2) + "M";
  if (t >= 1000) return (t / 1000).toFixed(1) + "k";
  return t.toString();
}

function formatRelativeTime(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  const now = new Date();
  const diffSec = Math.floor((now - d) / 1000);
  if (diffSec < 60) return "Just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  return `${Math.floor(diffSec / 86400)}d ago`;
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => {
    showToast("Key copied to clipboard!");
  }).catch(() => {
    alert("Key: " + text);
  });
}

function showToast(msg) {
  const toast = document.createElement("div");
  toast.textContent = msg;
  toast.style.position = "fixed";
  toast.style.bottom = "24px";
  toast.style.right = "24px";
  toast.style.background = "linear-gradient(135deg, #00F2FE, #4FACFE)";
  toast.style.color = "#000";
  toast.style.fontWeight = "700";
  toast.style.padding = "10px 20px";
  toast.style.borderRadius = "8px";
  toast.style.zIndex = "9999";
  toast.style.boxShadow = "0 8px 24px rgba(0, 242, 254, 0.4)";
  document.body.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 2200);
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
