/**
 * AI Gateway Control Plane — Frontend Module
 * Production Developer & Admin Console
 */

(function () {
  'use strict';

  // State Management
  let currentRange = '24h';
  let refreshIntervalMs = 15000;
  let refreshTimer = null;
  let lastSyncTime = null;
  let relativeTimeTimer = null;
  let isFetching = false;

  // DOM Elements Cache
  const elements = {
    // Top Controls
    rangeButtons: document.querySelectorAll('.seg-btn'),
    refreshSelect: document.getElementById('refresh-interval'),
    refreshBtn: document.getElementById('refresh-btn'),
    lastSyncText: document.getElementById('last-sync-text'),
    syncDot: document.getElementById('sync-dot'),
    mobileMenuBtn: document.getElementById('mobile-menu-btn'),
    sidebar: document.getElementById('sidebar'),
    sidebarBackdrop: document.getElementById('sidebar-backdrop'),
    gatewayStatusDot: document.getElementById('gateway-status-dot'),
    gatewayStatusLabel: document.getElementById('gateway-status-label'),
    gatewayStatusSub: document.getElementById('gateway-status-sub'),

    // KPI Values
    kpiTotalReqs: document.getElementById('kpi-total-reqs'),
    kpiTotalSub: document.getElementById('kpi-total-sub'),
    kpiSuccessRate: document.getElementById('kpi-success-rate'),
    kpiSuccessSub: document.getElementById('kpi-success-sub'),
    kpiAvgLatency: document.getElementById('kpi-avg-latency'),
    kpiLatencySub: document.getElementById('kpi-latency-sub'),
    kpiTotalTokens: document.getElementById('kpi-total-tokens'),
    kpiTokensSub: document.getElementById('kpi-tokens-sub'),

    // Chart
    trafficChartWrap: document.getElementById('timeseries-chart-wrap') || document.getElementById('traffic-chart-wrap'),
    trafficIntervalLabel: document.getElementById('traffic-interval-label'),
    chartTooltip: document.getElementById('chart-tooltip'),

    // Latency Distribution
    latencyAvg: document.getElementById('latency-stat-avg'),
    latencyMin: document.getElementById('latency-stat-min'),
    latencyMax: document.getElementById('latency-stat-max'),
    latencyDistWrap: document.getElementById('latency-dist-wrap'),

    // Error Breakdown
    errRateVal: document.getElementById('err-rate-val'),
    errGatewayVal: document.getElementById('err-gateway-val'),
    errBlockedVal: document.getElementById('err-blocked-val'),

    // Tables
    modelsTbody: document.getElementById('models-tbody'),
    recentTbody: document.getElementById('recent-tbody'),
    workersTbody: document.getElementById('workers-tbody'),

    // Policy Cards
    policyPublicBurst: document.getElementById('policy-public-burst'),
    policyPublicHourly: document.getElementById('policy-public-hourly'),
    policyAuthBurst: document.getElementById('policy-auth-burst'),
    policyAuthHourly: document.getElementById('policy-auth-hourly')
  };

  /**
   * Safe Fetch Utility with Timeout & JSON parsing
   */
  async function fetchJson(url) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 8000);
      const res = await fetch(url, { signal: controller.signal });
      clearTimeout(timeoutId);
      if (!res.ok) {
        console.warn(`[Gateway Dashboard] API returned status ${res.status} for ${url}`);
        return null;
      }
      return await res.json();
    } catch (err) {
      console.warn(`[Gateway Dashboard] Fetch error for ${url}:`, err);
      return null;
    }
  }

  /**
   * Deterministic Number Formatter
   */
  function formatNum(num) {
    if (num === null || num === undefined || isNaN(num)) return '0';
    return Number(num).toLocaleString();
  }

  /**
   * Relative time string calculation
   */
  function updateRelativeTime() {
    if (!lastSyncTime || !elements.lastSyncText) return;
    const diffSec = Math.max(0, Math.floor((Date.now() - lastSyncTime) / 1000));
    if (diffSec < 2) {
      elements.lastSyncText.textContent = 'Just now';
    } else if (diffSec < 60) {
      elements.lastSyncText.textContent = `${diffSec}s ago`;
    } else {
      const min = Math.floor(diffSec / 60);
      elements.lastSyncText.textContent = `${min}m ago`;
    }
  }

  /**
   * Main Dashboard Data Fetcher & Non-flickering Renderer
   */
  async function loadDashboardData() {
    if (isFetching) return;
    isFetching = true;

    if (elements.syncDot) elements.syncDot.classList.add('updating');

    try {
      // Parallel fetch of real backend analytics & system endpoints
      const [summary, traffic, latency, errors, models, recent, workersStatus, usage, auditLogs] = await Promise.all([
        fetchJson(`/api/analytics/summary?range=${currentRange}`),
        fetchJson(`/api/analytics/requests?range=${currentRange}`),
        fetchJson(`/api/analytics/latency?range=${currentRange}`),
        fetchJson(`/api/analytics/errors?range=${currentRange}`),
        fetchJson(`/api/analytics/models?range=${currentRange}`),
        fetchJson(`/api/analytics/recent?limit=20`),
        fetchJson('/status'),
        fetchJson('/api/usage'),
        fetchJson('/api/admin/audit-logs?limit=15')
      ]);

      // Update Gateway Status from real /status
      renderGatewayHealth(workersStatus);

      // Render 4 Primary KPIs
      renderKpis(summary);

      // Render Primary Traffic Activity Chart
      renderTrafficChart(traffic);

      // Render Latency & Performance Breakdown
      renderLatencyDistribution(latency);

      // Render Error Monitoring
      renderErrorBreakdown(errors);

      // Render Models Table
      renderModelsTable(models);

      // Render Recent Gateway Activity Table
      renderRecentActivity(recent);

      // Render Worker Nodes Cluster Table
      renderWorkersTable(workersStatus);

      // Render Usage Policy Reference
      renderUsagePolicy(usage);

      // Render Audit Logs (if admin or present)
      renderAuditLogs(auditLogs);

      lastSyncTime = Date.now();
      updateRelativeTime();
    } catch (e) {
      console.error('[Gateway Dashboard] Error updating control plane:', e);
    } finally {
      isFetching = false;
      if (elements.syncDot) elements.syncDot.classList.remove('updating');
    }
  }

  /**
   * Render Gateway Cluster Health Indicator
   */
  function renderGatewayHealth(statusData) {
    if (!elements.gatewayStatusDot || !elements.gatewayStatusLabel) return;

    if (!statusData || typeof statusData !== 'object') {
      elements.gatewayStatusDot.className = 'status-dot offline';
      elements.gatewayStatusLabel.textContent = 'Disconnected';
      elements.gatewayStatusSub.textContent = 'Gateway unavailable';
      return;
    }

    const workers = Array.isArray(statusData.workers)
      ? statusData.workers
      : (Array.isArray(statusData) ? statusData : Object.values(statusData));

    const totalWorkers = workers.length;
    const onlineWorkers = workers.filter(w => w && w.online).length;

    if (onlineWorkers === totalWorkers && totalWorkers > 0) {
      elements.gatewayStatusDot.className = 'status-dot';
      elements.gatewayStatusLabel.textContent = 'Operational';
      elements.gatewayStatusSub.textContent = `${onlineWorkers}/${totalWorkers} nodes active`;
    } else if (onlineWorkers > 0) {
      elements.gatewayStatusDot.className = 'status-dot degraded';
      elements.gatewayStatusLabel.textContent = 'Degraded';
      elements.gatewayStatusSub.textContent = `${onlineWorkers}/${totalWorkers} nodes active`;
    } else {
      elements.gatewayStatusDot.className = 'status-dot offline';
      elements.gatewayStatusLabel.textContent = 'Offline';
      elements.gatewayStatusSub.textContent = `0/${totalWorkers} nodes active`;
    }
  }

  /**
   * Render Primary 4 KPI Metrics
   */
  function renderKpis(summary) {
    if (!summary || !summary.requests) {
      if (elements.kpiTotalReqs) elements.kpiTotalReqs.textContent = '—';
      if (elements.kpiSuccessRate) elements.kpiSuccessRate.textContent = '—';
      if (elements.kpiAvgLatency) elements.kpiAvgLatency.textContent = '—';
      if (elements.kpiTotalTokens) elements.kpiTotalTokens.textContent = '—';
      return;
    }

    const reqs = summary.requests;
    const total = reqs.total || 0;
    const successful = reqs.successful || 0;
    const failed = reqs.failed || 0;
    const rateLimited = reqs.rate_limited || 0;

    // 1. Total Requests
    elements.kpiTotalReqs.textContent = formatNum(total);
    elements.kpiTotalSub.textContent = `${currentRange} traffic window`;

    // 2. Success Rate
    const successRate = total > 0 ? ((successful / total) * 100).toFixed(1) : '100.0';
    elements.kpiSuccessRate.textContent = `${successRate}%`;
    elements.kpiSuccessSub.textContent = `${formatNum(successful)} ok · ${formatNum(failed)} err · ${formatNum(rateLimited)} 429`;

    // 3. Average Latency
    const avgLat = summary.latency ? (summary.latency.average_ms || 0) : 0;
    const minLat = summary.latency ? (summary.latency.min_ms || 0) : 0;
    const maxLat = summary.latency ? (summary.latency.max_ms || 0) : 0;
    elements.kpiAvgLatency.textContent = `${avgLat} ms`;
    elements.kpiLatencySub.textContent = `Min ${minLat}ms · Max ${maxLat}ms`;

    // 4. Token Usage
    const totalTokens = summary.tokens ? (summary.tokens.total || 0) : 0;
    const avgTokens = summary.tokens ? Math.round(summary.tokens.avg_per_request || 0) : 0;
    elements.kpiTotalTokens.textContent = formatNum(totalTokens);
    elements.kpiTokensSub.textContent = `Avg ${formatNum(avgTokens)} tok / req`;
  }

  /**
   * Render Time-Series Traffic Chart with Precision Tooltip
   */
  function renderTrafficChart(trafficData) {
    const wrap = elements.trafficChartWrap;
    if (!wrap) return;

    if (!trafficData || !Array.isArray(trafficData.data) || trafficData.data.length === 0) {
      wrap.innerHTML = `
        <div class="state-container">
          <div class="state-title">No request activity recorded</div>
          <div class="state-desc">Inference requests matching range (${currentRange}) will appear here in real time.</div>
        </div>`;
      if (elements.trafficIntervalLabel) elements.trafficIntervalLabel.textContent = `${currentRange} window`;
      return;
    }

    const intervalSec = trafficData.interval_seconds || 3600;
    if (elements.trafficIntervalLabel) {
      if (intervalSec < 3600) {
        elements.trafficIntervalLabel.textContent = `${Math.round(intervalSec / 60)}m buckets`;
      } else if (intervalSec === 3600) {
        elements.trafficIntervalLabel.textContent = `1h buckets`;
      } else {
        elements.trafficIntervalLabel.textContent = `Daily buckets`;
      }
    }

    const data = trafficData.data;
    const maxReqs = Math.max(...data.map(d => d.requests || 0), 4);
    const rect = wrap.getBoundingClientRect();
    const w = Math.max(300, Math.floor(rect.width || wrap.clientWidth || 600));
    const h = 180;

    const padTop = 15;
    const padBottom = 22;
    const padLeft = 36;
    const padRight = 10;
    const chartW = w - padLeft - padRight;
    const chartH = h - padTop - padBottom;

    const barStep = chartW / data.length;
    const barW = Math.max(2, Math.min(18, barStep * 0.72));

    let svg = `<svg class="timeseries-svg" viewBox="0 0 ${w} ${h}">`;

    // Horizontal Grid Lines & Y-Axis Scale
    const yTicks = 4;
    for (let i = 0; i <= yTicks; i++) {
      const val = Math.round((maxReqs / yTicks) * i);
      const y = padTop + chartH - (chartH / yTicks) * i;
      svg += `<line class="chart-grid-line" x1="${padLeft}" y1="${y}" x2="${w - padRight}" y2="${y}" />`;
      svg += `<text class="chart-axis-label" x="${padLeft - 6}" y="${y + 3}" text-anchor="end">${val}</text>`;
    }

    // Stacked Bars (Successful, Rate-Limited, Failed)
    data.forEach((d, idx) => {
      const x = padLeft + idx * barStep + (barStep - barW) / 2;
      const totalCount = d.requests || 0;
      const totalH = (totalCount / maxReqs) * chartH;
      const baseY = padTop + chartH;

      if (totalCount > 0) {
        const successCount = d.successful || 0;
        const failedCount = d.failed || 0;
        const rlCount = d.rate_limited || 0;

        const successH = (successCount / maxReqs) * chartH;
        const rlH = (rlCount / maxReqs) * chartH;
        const failH = (failedCount / maxReqs) * chartH;

        let currentY = baseY;

        // Render Success portion
        if (successCount > 0) {
          currentY -= successH;
          svg += `<rect class="chart-bar-success" x="${x}" y="${currentY}" width="${barW}" height="${Math.max(1, successH)}" rx="1" data-idx="${idx}" />`;
        }

        // Render Rate Limited portion
        if (rlCount > 0) {
          currentY -= rlH;
          svg += `<rect class="chart-bar-ratelimit" x="${x}" y="${currentY}" width="${barW}" height="${Math.max(1, rlH)}" rx="1" data-idx="${idx}" />`;
        }

        // Render Failed portion
        if (failedCount > 0) {
          currentY -= failH;
          svg += `<rect class="chart-bar-error" x="${x}" y="${currentY}" width="${barW}" height="${Math.max(1, failH)}" rx="1" data-idx="${idx}" />`;
        }
      }

      // X-Axis Labels (sample cleanly)
      const labelInterval = Math.max(1, Math.ceil(data.length / (w > 600 ? 7 : 4)));
      if (idx % labelInterval === 0 || idx === data.length - 1) {
        const timeLabel = (d.timestamp || '').split(' ')[1] || d.timestamp || '';
        svg += `<text class="chart-axis-label" x="${x + barW / 2}" y="${h - 4}" text-anchor="middle">${timeLabel}</text>`;
      }
    });

    svg += `</svg>`;
    wrap.innerHTML = svg;

    // Attach Interactive Tooltip Hover Handlers
    const bars = wrap.querySelectorAll('rect[data-idx]');
    bars.forEach(bar => {
      bar.addEventListener('mouseenter', (e) => {
        const idx = parseInt(bar.getAttribute('data-idx'), 10);
        const item = data[idx];
        if (!item || !elements.chartTooltip) return;

        const tip = elements.chartTooltip;
        tip.innerHTML = `
          <div style="font-weight:600;margin-bottom:3px;color:var(--text-primary)">${item.timestamp}</div>
          <div style="color:var(--accent)">Requests: <b>${formatNum(item.requests)}</b></div>
          <div style="color:var(--text-secondary);font-size:10px;">
            ✓ ${formatNum(item.successful)} ok · ✗ ${formatNum(item.failed)} err · ⚠ ${formatNum(item.rate_limited)} 429
          </div>
          <div style="color:var(--text-muted);font-size:10px;margin-top:2px;">
            ${formatNum(item.tokens || 0)} tok · ${item.avg_latency_ms || 0} ms avg
          </div>`;
        tip.style.display = 'block';

        const wrapRect = wrap.getBoundingClientRect();
        const barRect = bar.getBoundingClientRect();
        const left = barRect.left - wrapRect.left + (barRect.width / 2);
        const top = barRect.top - wrapRect.top - 10;

        tip.style.left = `${Math.max(10, Math.min(left - 60, wrapRect.width - 150))}px`;
        tip.style.top = `${Math.max(0, top - 60)}px`;
      });

      bar.addEventListener('mouseleave', () => {
        if (elements.chartTooltip) elements.chartTooltip.style.display = 'none';
      });
    });
  }

  /**
   * Render Latency & Performance Breakdown
   */
  function renderLatencyDistribution(latencyData) {
    if (!elements.latencyAvg || !elements.latencyDistWrap) return;

    if (!latencyData) {
      elements.latencyAvg.textContent = '0 ms';
      elements.latencyMin.textContent = '0 ms';
      elements.latencyMax.textContent = '0 ms';
      elements.latencyDistWrap.innerHTML = '<div class="state-desc" style="padding:10px 0;">No latency metrics available.</div>';
      return;
    }

    elements.latencyAvg.textContent = `${latencyData.average_ms || 0} ms`;
    elements.latencyMin.textContent = `${latencyData.min_ms || 0} ms`;
    elements.latencyMax.textContent = `${latencyData.max_ms || 0} ms`;

    const dist = latencyData.distribution || {};
    const totalMeasured = latencyData.total_measured_requests || 1;
    const buckets = [
      { key: '< 500ms', label: '< 500ms' },
      { key: '500ms - 1s', label: '500ms – 1s' },
      { key: '1s - 3s', label: '1s – 3s' },
      { key: '3s - 5s', label: '3s – 5s' },
      { key: '> 5s', label: '> 5s' }
    ];

    let html = '';
    buckets.forEach(b => {
      const count = dist[b.key] || 0;
      const pct = totalMeasured > 0 ? ((count / totalMeasured) * 100).toFixed(1) : 0;
      html += `
        <div class="dist-row">
          <div class="dist-header">
            <span class="dist-label">${b.label}</span>
            <span class="dist-count">${formatNum(count)} (${pct}%)</span>
          </div>
          <div class="dist-bar-track">
            <div class="dist-bar-fill" style="width: ${pct}%"></div>
          </div>
        </div>`;
    });

    elements.latencyDistWrap.innerHTML = html;
  }

  /**
   * Render Error Monitoring Breakdown
   */
  function renderErrorBreakdown(errorData) {
    if (!elements.errRateVal) return;

    if (!errorData) {
      elements.errRateVal.textContent = '0.0%';
      elements.errGatewayVal.textContent = '0';
      elements.errBlockedVal.textContent = '0';
      return;
    }

    elements.errRateVal.textContent = `${errorData.error_rate || 0}%`;
    elements.errGatewayVal.textContent = formatNum(errorData.gateway_errors || 0);
    elements.errBlockedVal.textContent = formatNum(errorData.total_rate_limited || 0);
  }

  /**
   * Render Models Table
   */
  function renderModelsTable(modelsData) {
    const tbody = elements.modelsTbody;
    if (!tbody) return;

    if (!Array.isArray(modelsData) || modelsData.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="5" style="text-align:center;color:var(--text-muted);padding:24px;">
            No model traffic recorded in this range.
          </td>
        </tr>`;
      return;
    }

    tbody.innerHTML = modelsData.map(m => {
      const reqs = formatNum(m.requests || 0);
      const tokens = formatNum(m.tokens || 0);
      const lat = `${m.average_latency_ms || 0} ms`;
      const errRate = `${m.error_rate || 0}%`;
      const errColor = (m.error_rate > 0) ? 'var(--status-danger)' : 'var(--text-muted)';

      return `
        <tr>
          <td class="cell-primary cell-mono">${escapeHtml(m.model)}</td>
          <td class="num">${reqs}</td>
          <td class="num">${tokens}</td>
          <td class="num">${lat}</td>
          <td class="num" style="color:${errColor}">${errRate}</td>
        </tr>`;
    }).join('');
  }

  /**
   * Render Recent Gateway Activity Table
   */
  function renderRecentActivity(recentData) {
    const tbody = elements.recentTbody;
    if (!tbody) return;

    if (!Array.isArray(recentData) || recentData.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="7" style="text-align:center;color:var(--text-muted);padding:28px;">
            No recent activity recorded.
          </td>
        </tr>`;
      return;
    }

    tbody.innerHTML = recentData.map(r => {
      let badgeHtml = '<span class="badge badge-success">200 OK</span>';
      if (r.status === 'error') {
        badgeHtml = '<span class="badge badge-error">500 Error</span>';
      } else if (r.status === 'rate_limited') {
        badgeHtml = '<span class="badge badge-warning">429 Rate Limit</span>';
      }

      const timeFormatted = r.created_at ? new Date(r.created_at * 1000).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      }) : '—';

      const latText = (r.latency_ms && r.latency_ms > 0) ? `${r.latency_ms} ms` : '—';
      const tokenText = formatNum(r.tokens || 0);
      const intentText = escapeHtml(r.intent || 'chat');

      return `
        <tr>
          <td class="cell-mono">${timeFormatted}</td>
          <td class="cell-mono" title="${escapeHtml(r.identity || '')}">${escapeHtml(r.identity || 'anonymous')}</td>
          <td class="cell-primary cell-mono">${escapeHtml(r.model || 'unknown')}</td>
          <td>${badgeHtml}</td>
          <td class="num">${tokenText}</td>
          <td class="num">${latText}</td>
          <td><span class="badge badge-neutral">${intentText}</span></td>
        </tr>`;
    }).join('');
  }

  /**
   * Render Worker Cluster Nodes Table
   */
  function renderWorkersTable(statusData) {
    const tbody = elements.workersTbody;
    if (!tbody) return;

    if (!statusData || typeof statusData !== 'object') {
      tbody.innerHTML = `
        <tr>
          <td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px;">
            No worker nodes connected.
          </td>
        </tr>`;
      return;
    }

    const workers = Array.isArray(statusData.workers)
      ? statusData.workers
      : (Array.isArray(statusData) ? statusData : Object.entries(statusData).map(([role, s]) => ({ role, ...s })));

    if (workers.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px;">
            No worker nodes connected.
          </td>
        </tr>`;
      return;
    }

    tbody.innerHTML = workers.map(s => {
      const isOnline = s && s.online;
      const badge = isOnline
        ? '<span class="badge badge-success">Online</span>'
        : '<span class="badge badge-error">Offline</span>';

      const latVal = (s && (s.latency_ms !== undefined || s.latency !== undefined))
        ? (s.latency_ms !== undefined ? s.latency_ms : s.latency)
        : null;
      const lat = (latVal !== null && latVal > 0) ? `${latVal} ms` : (isOnline ? '0 ms' : '—');
      const reqCount = (s && s.requests !== undefined) ? formatNum(s.requests) : '0';
      const queueCount = (s && s.queue !== undefined) ? s.queue : '0';
      const modelsStr = (s && Array.isArray(s.models)) ? s.models.slice(0, 3).join(', ') : '—';
      const role = s.role || 'node';

      return `
        <tr>
          <td class="cell-primary cell-mono">${escapeHtml(role)}</td>
          <td>${badge}</td>
          <td class="num">${queueCount}</td>
          <td class="num">${lat}</td>
          <td class="num">${reqCount}</td>
          <td class="cell-mono" style="font-size:11px;color:var(--text-muted);">${escapeHtml(modelsStr)}</td>
        </tr>`;
    }).join('');
  }

  /**
   * Render Rate Limiter Policy & Quota Reference
   */
  function renderUsagePolicy(usageData) {
    if (!elements.policyPublicBurst) return;

    if (usageData) {
      const burstLimit = (usageData.burst && usageData.burst.limit) || usageData.burst_limit || 10;
      const burstWindow = (usageData.burst && usageData.burst.window_sec) || 60;
      const hourlyLimit = (usageData.hourly && usageData.hourly.limit) || usageData.hourly_limit || 20;

      elements.policyPublicBurst.textContent = `10 / 60s`;
      elements.policyPublicHourly.textContent = `20 / hr`;
      elements.policyAuthBurst.textContent = `10 / 60s`;
      elements.policyAuthHourly.textContent = `200 / hr`;
    }
  }

  /**
   * Render Audit & Security Logs Table (Admin)
   */
  function renderAuditLogs(auditData) {
    const tbody = document.getElementById('audit-tbody');
    if (!tbody) return;

    if (!auditData || !Array.isArray(auditData.logs) || auditData.logs.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="7" style="text-align:center;color:var(--text-muted);padding:24px;">
            No audit events recorded yet or admin authorization required.
          </td>
        </tr>`;
      return;
    }

    tbody.innerHTML = auditData.logs.map(l => {
      let resultBadge = '<span class="badge badge-success">Success</span>';
      if (l.result === 'failed') resultBadge = '<span class="badge badge-error">Failed</span>';
      else if (l.result === 'denied') resultBadge = '<span class="badge badge-warning">Denied</span>';

      const timeFormatted = l.timestamp ? new Date(l.timestamp * 1000).toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit', second: '2-digit'
      }) : '—';

      return `
        <tr>
          <td class="cell-mono">${timeFormatted}</td>
          <td class="cell-primary cell-mono">${escapeHtml(l.actor_id || '')}</td>
          <td class="cell-mono"><b>${escapeHtml(l.action || '')}</b></td>
          <td>${escapeHtml(l.resource_type || '')}</td>
          <td>${resultBadge}</td>
          <td class="cell-mono">${escapeHtml(l.ip_address || '—')}</td>
          <td class="cell-mono" style="font-size:11px;color:var(--text-muted);">${escapeHtml((l.request_id || '').slice(0, 16))}</td>
        </tr>`;
    }).join('');
  }

  /**
   * Helper: Escape HTML to avoid XSS
   */
  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  /**
   * Event Handlers Setup
   */
  function setupEventListeners() {
    // Range selector buttons (1h, 24h, 7d, 30d)
    elements.rangeButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        elements.rangeButtons.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentRange = btn.getAttribute('data-range') || '24h';
        loadDashboardData();
      });
    });

    // Refresh button
    if (elements.refreshBtn) {
      elements.refreshBtn.addEventListener('click', () => {
        loadDashboardData();
      });
    }

    // Auto-refresh Interval Selector
    if (elements.refreshSelect) {
      elements.refreshSelect.addEventListener('change', () => {
        if (refreshTimer) clearInterval(refreshTimer);
        refreshIntervalMs = parseInt(elements.refreshSelect.value, 10);
        if (refreshIntervalMs > 0) {
          refreshTimer = setInterval(loadDashboardData, refreshIntervalMs);
        }
      });
    }

    // Mobile Sidebar Drawer Toggle
    if (elements.mobileMenuBtn && elements.sidebar && elements.sidebarBackdrop) {
      elements.mobileMenuBtn.addEventListener('click', () => {
        elements.sidebar.classList.toggle('open');
        elements.sidebarBackdrop.classList.toggle('active');
      });

      elements.sidebarBackdrop.addEventListener('click', () => {
        elements.sidebar.classList.remove('open');
        elements.sidebarBackdrop.classList.remove('active');
      });
    }

    // Window Resize Chart Relayout
    window.addEventListener('resize', () => {
      // Re-trigger data fetch to resize SVG cleanly
      fetchJson(`/api/analytics/requests?range=${currentRange}`).then(traffic => {
        if (traffic) renderTrafficChart(traffic);
      });
    });
  }

  // Initialize Application
  function init() {
    setupEventListeners();
    loadDashboardData();

    // Setup Auto Refresh timer
    if (refreshIntervalMs > 0) {
      refreshTimer = setInterval(loadDashboardData, refreshIntervalMs);
    }

    // Setup Relative Time live updater every 3 seconds
    relativeTimeTimer = setInterval(updateRelativeTime, 3000);
  }

  // Run on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
