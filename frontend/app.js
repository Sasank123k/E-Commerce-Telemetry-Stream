/* ═══════════════════════════════════════════════════════════════════
   E-Commerce Telemetry Stream — Dashboard Logic
   WebSocket connection · Live metric updates · API controls
   ═══════════════════════════════════════════════════════════════════ */

const API_BASE = "http://127.0.0.1:8000";
const WS_URL   = "ws://127.0.0.1:8000/ws/metrics";

// ─── DOM references ───
const dom = {
    // Header
    connectionBadge:   document.getElementById("connectionBadge"),
    connectionDot:     document.getElementById("connectionDot"),
    connectionText:    document.getElementById("connectionText"),
    uptimeValue:       document.getElementById("uptimeValue"),

    // KPI cards
    totalEventsProcessed: document.getElementById("totalEventsProcessed"),
    totalRevenue:         document.getElementById("totalRevenue"),
    eventsPerSec:         document.getElementById("eventsPerSec"),
    queueBacklog:         document.getElementById("queueBacklog"),
    activeWorkers:        document.getElementById("activeWorkers"),
    activeClients:        document.getElementById("activeClients"),

    // Breakdown
    revenueByCategory: document.getElementById("revenueByCategory"),
    revenueByRegion:   document.getElementById("revenueByRegion"),

    // Controls
    rateSlider:        document.getElementById("rateSlider"),
    rateInput:         document.getElementById("rateInput"),
    rateCurrentBadge:  document.getElementById("rateCurrentBadge"),

    workersSlider:        document.getElementById("workersSlider"),
    workersInput:         document.getElementById("workersInput"),
    workersCurrentBadge:  document.getElementById("workersCurrentBadge"),

    heavyCheckbox:        document.getElementById("heavyCheckbox"),
    heavyLabel:           document.getElementById("heavyLabel"),
    heavyCurrentBadge:    document.getElementById("heavyCurrentBadge"),

    playPauseBtn:         document.getElementById("playPauseBtn"),
    playPauseLabel:       document.getElementById("playPauseLabel"),
    streamStateBadge:     document.getElementById("streamStateBadge"),

    toastContainer: document.getElementById("toastContainer"),
};

// ─── Formatting helpers ───
function formatNumber(n) {
    if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(1) + "B";
    if (n >= 1_000_000)     return (n / 1_000_000).toFixed(1) + "M";
    return Math.round(n).toLocaleString();
}

function formatCurrency(n) {
    if (n >= 1_000_000_000) return "$" + (n / 1_000_000_000).toFixed(2) + "B";
    if (n >= 1_000_000)     return "$" + (n / 1_000_000).toFixed(2) + "M";
    if (n >= 1_000)         return "$" + (n / 1_000).toFixed(1) + "K";
    return "$" + n.toFixed(2);
}

function formatUptime(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

// ─── Flash animation on value change ───
function flashMetric(el) {
    el.classList.add("metric-card__value--flash");
    clearTimeout(el._flashTimer);
    el._flashTimer = setTimeout(() => el.classList.remove("metric-card__value--flash"), 300);
}

function setMetric(el, value) {
    if (el.textContent !== value) {
        el.textContent = value;
        flashMetric(el);
    }
}

// ─── Revenue breakdown rendering ───
function renderBreakdown(container, dataObj) {
    const entries = Object.entries(dataObj).sort((a, b) => b[1] - a[1]);
    const maxVal  = entries.length ? entries[0][1] : 1;

    // Reuse existing DOM if structure matches
    const existingItems = container.querySelectorAll(".breakdown-item");
    if (existingItems.length !== entries.length) {
        container.innerHTML = entries.map(([name]) => `
            <div class="breakdown-item">
                <span class="breakdown-item__name">${name}</span>
                <div class="breakdown-item__bar-wrap">
                    <div class="breakdown-item__bar" style="width:0%"></div>
                </div>
                <span class="breakdown-item__value">$0</span>
            </div>
        `).join("");
    }

    const items = container.querySelectorAll(".breakdown-item");
    entries.forEach(([name, val], i) => {
        const pct = maxVal > 0 ? (val / maxVal) * 100 : 0;
        const item = items[i];
        if (item) {
            item.querySelector(".breakdown-item__name").textContent = name;
            item.querySelector(".breakdown-item__bar").style.width = pct + "%";
            item.querySelector(".breakdown-item__value").textContent = formatCurrency(val);
        }
    });
}

// ─── Toast notifications ───
function showToast(message, type = "info") {
    const toast = document.createElement("div");
    toast.className = `toast toast--${type}`;

    const icons = {
        success: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`,
        error:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
        info:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
    };

    toast.innerHTML = `${icons[type] || icons.info}<span>${message}</span>`;
    dom.toastContainer.appendChild(toast);

    setTimeout(() => {
        toast.classList.add("toast--out");
        toast.addEventListener("animationend", () => toast.remove());
    }, 3000);
}

// ═══════════════════════════════════════════════════════════════════
// WebSocket connection with auto-reconnect
// ═══════════════════════════════════════════════════════════════════

let ws = null;
let reconnectDelay = 1000;
let reconnectTimer = null;

function setConnectionStatus(connected) {
    if (connected) {
        dom.connectionBadge.classList.add("connection-badge--connected");
        dom.connectionText.textContent = "Live";
    } else {
        dom.connectionBadge.classList.remove("connection-badge--connected");
        dom.connectionText.textContent = "Disconnected";
    }
}

function connectWebSocket() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }

    dom.connectionText.textContent = "Connecting...";
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        setConnectionStatus(true);
        reconnectDelay = 1000;
        showToast("Connected to telemetry stream", "success");
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            updateDashboard(data);
        } catch (e) {
            console.error("[ws] Parse error:", e);
        }
    };

    ws.onclose = () => {
        setConnectionStatus(false);
        scheduleReconnect();
    };

    ws.onerror = () => {
        ws.close();
    };
}

function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
        dom.connectionText.textContent = "Reconnecting...";
        connectWebSocket();
        reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
    }, reconnectDelay);
}

// ═══════════════════════════════════════════════════════════════════
// Dashboard update from WebSocket payload
// ═══════════════════════════════════════════════════════════════════

function updateDashboard(data) {
    // KPIs
    setMetric(dom.totalEventsProcessed, formatNumber(data.total_events_processed));
    setMetric(dom.totalRevenue, formatCurrency(data.total_revenue));

    if (data.system) {
        setMetric(dom.eventsPerSec,  formatNumber(data.system.events_processed_per_sec));
        setMetric(dom.queueBacklog,  formatNumber(data.system.queue_backlog_size));
        setMetric(dom.activeWorkers, String(data.system.active_workers));
        setMetric(dom.activeClients, String(data.system.active_ws_clients));

        // Uptime
        dom.uptimeValue.textContent = formatUptime(data.system.server_uptime_sec || 0);

        // Sync control badges with server state
        dom.rateCurrentBadge.textContent    = `Current: ${data.system.target_eps}`;
        dom.workersCurrentBadge.textContent = `Current: ${data.system.active_workers}`;

        const heavy = data.system.heavy_computation;
        dom.heavyCurrentBadge.textContent = heavy ? "ON" : "OFF";
        dom.heavyCheckbox.checked = heavy;
        dom.heavyLabel.textContent = heavy ? "Enabled" : "Disabled";

        // Sync stream state badge + local _isRunning from server truth
        if (typeof data.system.is_running !== "undefined") {
            const running = data.system.is_running;
            _isRunning = running;  // always sync from server
            dom.streamStateBadge.textContent = running ? "Running" : "Paused";
            dom.playPauseLabel.textContent = running ? "Pause" : "Resume";
        }
    }

    // Revenue breakdowns
    if (data.revenue_by_category) renderBreakdown(dom.revenueByCategory, data.revenue_by_category);
    if (data.revenue_by_region)   renderBreakdown(dom.revenueByRegion, data.revenue_by_region);
}

// ═══════════════════════════════════════════════════════════════════
// Control Panel — slider sync + API calls
// ═══════════════════════════════════════════════════════════════════

// Keep slider and number input in sync
dom.rateSlider.addEventListener("input", () => { dom.rateInput.value = dom.rateSlider.value; });
dom.rateInput.addEventListener("input",  () => { dom.rateSlider.value = dom.rateInput.value; });

dom.workersSlider.addEventListener("input", () => { dom.workersInput.value = dom.workersSlider.value; });
dom.workersInput.addEventListener("input",  () => { dom.workersSlider.value = dom.workersInput.value; });

// API helper
async function postConfig(endpoint, body, successMsg) {
    try {
        const res = await fetch(`${API_BASE}${endpoint}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            const err = await res.text();
            throw new Error(err || res.statusText);
        }

        const result = await res.json();
        showToast(successMsg, "success");
        return result;
    } catch (e) {
        showToast(`Failed: ${e.message}`, "error");
        console.error("[api]", e);
    }
}

// Exposed to onclick handlers in HTML
function applyRate() {
    const val = parseInt(dom.rateInput.value, 10);
    if (isNaN(val) || val < 100 || val > 10000000) {
        showToast("Rate must be between 100 and 10,000,000", "error");
        return;
    }
    postConfig("/config/rate", { target_eps: val }, `Event rate set to ${val.toLocaleString()} eps`);
}

function applyWorkers() {
    const val = parseInt(dom.workersInput.value, 10);
    if (isNaN(val) || val < 1 || val > 20) {
        showToast("Worker count must be between 1 and 20", "error");
        return;
    }
    postConfig("/config/workers", { worker_count: val }, `Worker count set to ${val}`);
}

function applyHeavy() {
    const on = dom.heavyCheckbox.checked;
    postConfig("/config/metrics", { heavy_computation: on },
        on ? "Heavy computation enabled" : "Heavy computation disabled"
    );
}

// Synced from WebSocket — always reflects actual server state
let _isRunning = true;

function togglePlayPause() {
    const newState = !_isRunning;
    postConfig("/config/state", { is_running: newState },
        newState ? "Stream resumed" : "Stream paused"
    ).then(() => {
        // Also switch back to random mode when resuming from live page
        if (newState) {
            postConfig("/config/source", { source: "random" }, "");
        }
    });
}

function resetMetrics() {
    postConfig("/config/reset", {}, "Metrics reset to zero");
}

// ═══════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════

connectWebSocket();
