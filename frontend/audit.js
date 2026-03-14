/**
 * audit.js — Audit Suite for E-Commerce Telemetry Stream.
 *
 * Manages WebSocket connection, audit test lifecycle, progress tracking,
 * and baseline validation with exact revenue matching.
 */

const WS_URL  = `ws://${location.hostname}:8000/ws/metrics`;
const API_BASE = `http://${location.hostname}:8000`;

// ═══════════════════════════════════════════════════════════════════
// DOM References
// ═══════════════════════════════════════════════════════════════════

const dom = {
    connectionBadge: document.getElementById("connectionBadge"),
    connectionDot:   document.getElementById("connectionDot"),
    connectionText:  document.getElementById("connectionText"),

    auditTotalEvents: document.getElementById("auditTotalEvents"),
    auditTargetEps:   document.getElementById("auditTargetEps"),
    auditStartBtn:    document.getElementById("auditStartBtn"),

    progressPct:     document.getElementById("progressPct"),
    progressBarFill: document.getElementById("progressBarFill"),
    progressDetail:  document.getElementById("progressDetail"),

    auditEps:     document.getElementById("auditEps"),
    auditRevenue: document.getElementById("auditRevenue"),
    auditQueue:   document.getElementById("auditQueue"),

    validationPanel:  document.getElementById("validationPanel"),
    validationIcon:   document.getElementById("validationIcon"),
    validationTitle:  document.getElementById("validationTitle"),
    validationDetail: document.getElementById("validationDetail"),

    toastContainer: document.getElementById("toastContainer"),
};

// ═══════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════

let _auditActive      = false;
let _auditTotalEvents = 0;
let _auditComplete    = false;

// ═══════════════════════════════════════════════════════════════════
// Formatting
// ═══════════════════════════════════════════════════════════════════

function formatNumber(n) {
    if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(1) + "B";
    if (n >= 1_000_000)     return (n / 1_000_000).toFixed(1) + "M";
    return n.toLocaleString();
}

function formatRevenue(n) {
    if (n >= 1_000_000_000) return "$" + (n / 1_000_000_000).toFixed(2) + "B";
    if (n >= 1_000_000)     return "$" + (n / 1_000_000).toFixed(2) + "M";
    return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ═══════════════════════════════════════════════════════════════════
// Toast Notifications
// ═══════════════════════════════════════════════════════════════════

function showToast(message, type = "info") {
    const el = document.createElement("div");
    el.className = `toast toast--${type}`;
    el.textContent = message;
    dom.toastContainer.appendChild(el);
    setTimeout(() => { el.classList.add("toast--out"); setTimeout(() => el.remove(), 200); }, 3000);
}

// ═══════════════════════════════════════════════════════════════════
// WebSocket Connection
// ═══════════════════════════════════════════════════════════════════

let _ws = null;
let _reconnectTimer = null;
const RECONNECT_DELAY = 2000;

function connectWebSocket() {
    _ws = new WebSocket(WS_URL);

    _ws.onopen = () => {
        dom.connectionBadge.classList.add("connection-badge--connected");
        dom.connectionText.textContent = "Connected";
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
    };

    _ws.onclose = () => {
        dom.connectionBadge.classList.remove("connection-badge--connected");
        dom.connectionText.textContent = "Reconnecting...";
        _reconnectTimer = setTimeout(connectWebSocket, RECONNECT_DELAY);
    };

    _ws.onerror = () => _ws.close();
    _ws.onmessage = (evt) => handleMetrics(JSON.parse(evt.data));
}

// ═══════════════════════════════════════════════════════════════════
// Metrics Handler
// ═══════════════════════════════════════════════════════════════════

function handleMetrics(data) {
    if (!_auditActive) return;

    const processed = data.total_events_processed || 0;
    const revenue   = data.total_revenue || 0;
    const queueSize = data.system?.queue_backlog_size || 0;
    const eps       = data.system?.events_processed_per_sec || 0;

    // Update live stats
    dom.auditEps.textContent     = formatNumber(Math.round(eps));
    dom.auditRevenue.textContent = formatRevenue(revenue);
    dom.auditQueue.textContent   = formatNumber(queueSize);

    // Update progress
    const pct = _auditTotalEvents > 0 ? Math.min(100, (processed / _auditTotalEvents) * 100) : 0;
    dom.progressPct.textContent       = pct.toFixed(1) + "%";
    dom.progressBarFill.style.width   = pct.toFixed(1) + "%";
    dom.progressDetail.textContent    = `${processed.toLocaleString()} / ${_auditTotalEvents.toLocaleString()}`;

    // Check completion: all events processed AND queue drained
    if (!_auditComplete && processed >= _auditTotalEvents && queueSize === 0) {
        _auditComplete = true;
        _auditActive   = false;
        dom.auditStartBtn.disabled = false;
        dom.auditStartBtn.innerHTML = `
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            Start Audit Test
        `;

        // Final revenue from the WS payload
        const actualRevenue = revenue;
        validateResults(actualRevenue);
    }
}

// ═══════════════════════════════════════════════════════════════════
// Validation
// ═══════════════════════════════════════════════════════════════════

async function validateResults(actualRevenue) {
    dom.validationIcon.textContent  = "⏳";
    dom.validationTitle.textContent = "Fetching baseline...";
    dom.validationTitle.style.color = "var(--text-secondary)";
    dom.validationPanel.className   = "validation-panel";

    try {
        const res = await fetch(`${API_BASE}/debug/baseline`);
        if (!res.ok) throw new Error("Baseline not available");
        const baseline = await res.json();

        // Multiplier: total_events / 10000 (baseline file has 10K events)
        const baselineEvents = baseline.total_events || 10000;
        const multiplier     = _auditTotalEvents / baselineEvents;
        const expectedRevenue = round2(baseline.total_revenue * multiplier);
        const actualRounded   = round2(actualRevenue);
        const diff            = round2(actualRounded - expectedRevenue);

        if (actualRounded === expectedRevenue) {
            // ✅ PASS
            dom.validationPanel.className = "validation-panel validation-panel--pass";
            dom.validationIcon.textContent  = "✅";
            dom.validationTitle.textContent = "PASS — Perfect Match";
            dom.validationTitle.style.color = "var(--accent-green)";
            dom.validationDetail.innerHTML  = `
                <div>Expected:  <strong>${formatRevenue(expectedRevenue)}</strong></div>
                <div>Actual:    <strong>${formatRevenue(actualRounded)}</strong></div>
                <div>Multiplier: <strong>${multiplier.toLocaleString()}×</strong> (${_auditTotalEvents.toLocaleString()} / ${baselineEvents.toLocaleString()})</div>
                <div style="margin-top:0.5rem; color: var(--accent-green);">Revenue matches exactly — zero floating-point drift detected.</div>
            `;
        } else {
            // ❌ FAIL
            dom.validationPanel.className = "validation-panel validation-panel--fail";
            dom.validationIcon.textContent  = "❌";
            dom.validationTitle.textContent = "FAIL — Revenue Mismatch";
            dom.validationTitle.style.color = "var(--accent-red)";
            dom.validationDetail.innerHTML  = `
                <div>Expected:  <strong>${formatRevenue(expectedRevenue)}</strong></div>
                <div>Actual:    <strong>${formatRevenue(actualRounded)}</strong></div>
                <div>Diff:      <strong style="color: var(--accent-red);">${diff >= 0 ? "+" : ""}${formatRevenue(diff)}</strong></div>
                <div>Multiplier: <strong>${multiplier.toLocaleString()}×</strong></div>
            `;
        }

    } catch (e) {
        dom.validationIcon.textContent  = "⚠️";
        dom.validationTitle.textContent = "Validation Error";
        dom.validationTitle.style.color = "var(--accent-amber)";
        dom.validationDetail.textContent = e.message;
    }
}

function round2(n) {
    return Math.round(n * 100) / 100;
}

// ═══════════════════════════════════════════════════════════════════
// Start Audit Test
// ═══════════════════════════════════════════════════════════════════

async function startAuditTest() {
    const totalEvents = parseInt(dom.auditTotalEvents.value, 10);
    const targetEps   = parseInt(dom.auditTargetEps.value, 10);

    if (isNaN(totalEvents) || totalEvents < 1000 || totalEvents > 100000000) {
        showToast("Total events must be between 1,000 and 100,000,000", "error");
        return;
    }
    if (isNaN(targetEps) || targetEps < 100 || targetEps > 10000000) {
        showToast("Target EPS must be between 100 and 10,000,000", "error");
        return;
    }

    // Reset UI state
    _auditTotalEvents = totalEvents;
    _auditComplete    = false;
    _auditActive      = true;

    dom.auditStartBtn.disabled = true;
    dom.auditStartBtn.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="animation: spin 1s linear infinite;"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
        Running…
    `;

    dom.progressPct.textContent     = "0%";
    dom.progressBarFill.style.width = "0%";
    dom.progressDetail.textContent  = `0 / ${totalEvents.toLocaleString()}`;

    dom.validationPanel.className    = "validation-panel";
    dom.validationIcon.textContent   = "🔬";
    dom.validationTitle.textContent  = "Test running…";
    dom.validationTitle.style.color  = "var(--text-muted)";
    dom.validationDetail.textContent = `Processing ${totalEvents.toLocaleString()} events at ${targetEps.toLocaleString()} eps`;

    dom.auditEps.textContent     = "0";
    dom.auditRevenue.textContent = "$0";
    dom.auditQueue.textContent   = "0";

    // Call the backend audit endpoint
    try {
        const res = await fetch(`${API_BASE}/audit/run`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ total_events: totalEvents, target_eps: targetEps }),
        });

        if (!res.ok) {
            const err = await res.text();
            throw new Error(err || res.statusText);
        }

        showToast("Audit test started", "success");

    } catch (e) {
        showToast(`Failed to start audit: ${e.message}`, "error");
        _auditActive = false;
        dom.auditStartBtn.disabled = false;
        dom.auditStartBtn.innerHTML = `
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            Start Audit Test
        `;
    }
}

// Spin animation for the running icon
const style = document.createElement("style");
style.textContent = `@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`;
document.head.appendChild(style);

// ═══════════════════════════════════════════════════════════════════
// API helper (for future use)
// ═══════════════════════════════════════════════════════════════════

async function postConfig(endpoint, body, successMsg) {
    try {
        const res = await fetch(`${API_BASE}${endpoint}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(await res.text());
        showToast(successMsg, "success");
    } catch (e) {
        showToast(`Failed: ${e.message}`, "error");
    }
}

// ═══════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════

connectWebSocket();
