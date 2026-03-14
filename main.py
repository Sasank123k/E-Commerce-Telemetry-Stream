"""
main.py -- Real-Time E-Commerce Telemetry Stream Backend.

Phase 1: FastAPI app initialisation, shared state, concurrency primitives.
Phase 2: High-frequency async event generator with queue.
Phase 3: Concurrent consumer workers with lock-protected aggregation.
Phase 4: WebSocket streaming endpoint with broadcast every 200ms.
Phase 5: Dynamic system controls (rate, workers, metrics toggle).

Dual-Mode Architecture:
- "random" mode: generates fresh events via Pydantic model (chaos testing)
- "static" mode: slices from pre-loaded test_data.json pool (deterministic auditing)
Both modes push raw dicts to the queue; consumers handle both single dicts and chunks.
"""

import asyncio
import json
import os
import random
import time
import threading # Added threading import
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from models import TransactionEvent

# ---------------------------------------------------------------------------
# Constants — realistic mock-data pools
# ---------------------------------------------------------------------------
CATEGORIES = ["Electronics", "Clothing", "Home & Kitchen", "Books", "Sports", "Toys", "Beauty", "Automotive"]
REGIONS = ["North America", "Europe", "Asia Pacific", "South America", "Middle East", "Africa"]
PRICE_RANGES = {
    "Electronics":      (19.99, 1499.99),
    "Clothing":         (9.99, 299.99),
    "Home & Kitchen":   (4.99, 599.99),
    "Books":            (2.99, 49.99),
    "Sports":           (14.99, 499.99),
    "Toys":             (4.99, 149.99),
    "Beauty":           (3.99, 199.99),
    "Automotive":       (9.99, 799.99),
}

BROADCAST_INTERVAL = 0.2  # seconds (200ms)

# ---------------------------------------------------------------------------
# Phase 5: Mutable runtime configuration
# ---------------------------------------------------------------------------
runtime_config = {
    "target_eps": 3_000,          # events per second (adjustable via API)
    "heavy_computation": False,   # toggle for simulated heavy processing
    "data_source": "random",      # "random" or "static" (dual-mode toggle)
    "is_running": True,           # pause/resume the generator
    "audit_target_remaining": 0,  # bounded test: events left to generate (0 = unlimited)
}

# ---------------------------------------------------------------------------
# Global shared state  (Phase 1.3 & 1.4)
# ---------------------------------------------------------------------------
shared_state: dict = {
    "total_events_processed": 0,
    "total_revenue": 0.0,
    "revenue_by_category": {cat: 0.0 for cat in CATEGORIES},
    "revenue_by_region": {reg: 0.0 for reg in REGIONS},
}

state_lock = asyncio.Lock()          # Phase 1.4 — protects shared_state
event_queue: asyncio.Queue = asyncio.Queue()   # Phase 2.1

# ---------------------------------------------------------------------------
# Worker task tracking  (Phase 5 — dynamic scaling)
# ---------------------------------------------------------------------------
_worker_tasks: dict[int, asyncio.Task] = {}   # worker_id → Task
_next_worker_id: int = 1                      # monotonically increasing ID

# ---------------------------------------------------------------------------
# Throughput tracking (for debug + broadcast payloads)
# ---------------------------------------------------------------------------
_gen_stats = {
    "events_generated": 0,
    "generator_running": False,
    "gen_start_time": 0.0,
}


class SlidingRateTracker:
    """
    Tracks a monotonically increasing counter and computes a smoothed
    per-second rate over a sliding window (default 3 seconds).

    Stores (timestamp, counter_value) samples in a deque and drops
    entries older than `window_sec`.  The rate is always computed as
    (newest - oldest) / (t_newest - t_oldest), which eliminates the
    200ms-interval spike artefact.
    """

    def __init__(self, window_sec: float = 3.0):
        self.window_sec = window_sec
        self._samples: deque[tuple[float, int]] = deque()
        self.rate: float = 0.0  # latest computed rate
        self._lock = threading.Lock() # Added threading lock

    def push(self, now: float, counter: int) -> float:
        """Record a sample and return the smoothed rate."""
        with self._lock: # Protected with lock
            self._samples.append((now, counter))

            # Evict samples older than the window
            cutoff = now - self.window_sec
            while len(self._samples) > 2 and self._samples[0][0] < cutoff:
                self._samples.popleft()

            # Need at least two samples to compute a rate
            if len(self._samples) >= 2:
                t0, c0 = self._samples[0]
                t1, c1 = self._samples[-1]
                dt = t1 - t0
                self.rate = round((c1 - c0) / dt, 1) if dt > 0 else 0.0
            return self.rate

    def clear(self):
        """Thread-safe clear of all samples."""
        with self._lock:
            self._samples.clear()
            self.rate = 0.0


_processed_rate_tracker = SlidingRateTracker(window_sec=3.0)
_generated_rate_tracker = SlidingRateTracker(window_sec=3.0)

# ---------------------------------------------------------------------------
# Pre-computed Event Pool (extreme perf for static mode)
# ---------------------------------------------------------------------------
_EVENT_POOL_SIZE = 10_000

def _build_event_pool() -> list[dict]:
    """Create a static pool of raw-dict events at startup."""
    pool = []
    for _ in range(_EVENT_POOL_SIZE):
        cat = random.choice(CATEGORIES)
        lo, hi = PRICE_RANGES[cat]
        pool.append({
            "price": round(random.uniform(lo, hi), 2),
            "quantity": random.randint(1, 5),
            "category": cat,
            "region": random.choice(REGIONS),
        })
    return pool

_EVENT_POOL: list[dict] = _build_event_pool()

# Static testing pool (loaded from test_data.json in lifespan, if available)
_STATIC_POOL: list[dict] = []
_STATIC_BASELINE: dict = {}


# ---------------------------------------------------------------------------
# Random Event Generator (chaos testing -- uses Pydantic model)
# ---------------------------------------------------------------------------

def _make_random_event() -> TransactionEvent:
    """Build a single randomised transaction event."""
    category = random.choice(CATEGORIES)
    lo, hi = PRICE_RANGES[category]
    return TransactionEvent(
        event_id=uuid4(),
        timestamp=datetime.now().timestamp(),
        user_id=f"user_{random.randint(1, 10_000)}",
        product_id=f"prod_{random.randint(1, 5_000)}",
        category=category,
        region=random.choice(REGIONS),
        price=round(random.uniform(lo, hi), 2),
        quantity=random.randint(1, 5),
    )


# ---------------------------------------------------------------------------
# Dual-Mode Event Generator
# ---------------------------------------------------------------------------

async def generate_events() -> None:
    """
    Dual-mode event generator with token-bucket rate limiting.
    Supports pause/resume via runtime_config["is_running"] and
    bounded tests via runtime_config["audit_target_remaining"].
    """
    _gen_stats["generator_running"] = True
    _gen_stats["gen_start_time"] = time.time()

    CHUNKS_PER_SEC = 20

    print(f"[generator] Starting -- initial target rate: {runtime_config['target_eps']} eps")

    epoch_start = time.monotonic()
    epoch_count = 0
    pool_idx = 0

    try:
        while True:
            # --- Pause check ---
            if not runtime_config["is_running"]:
                await asyncio.sleep(0.1)
                epoch_start = time.monotonic()
                epoch_count = 0
                continue

            target = runtime_config["target_eps"]
            source = runtime_config["data_source"]
            audit_rem = runtime_config["audit_target_remaining"]

            if source == "random":
                # --- RANDOM MODE ---
                event = _make_random_event()
                evt_dict = {
                    "price": event.price,
                    "quantity": event.quantity,
                    "category": event.category,
                    "region": event.region,
                }
                await event_queue.put(evt_dict)
                _gen_stats["events_generated"] += 1
                epoch_count += 1

                # Bounded test accounting
                if audit_rem > 0:
                    runtime_config["audit_target_remaining"] -= 1
                    if runtime_config["audit_target_remaining"] <= 0:
                        runtime_config["audit_target_remaining"] = 0
                        runtime_config["is_running"] = False
                        print("[generator] Audit target reached -- auto-paused")

            else:
                # --- STATIC MODE ---
                pool = _STATIC_POOL if _STATIC_POOL else _EVENT_POOL
                pool_len = len(pool)
                chunk_size = max(100, min(50_000, target // CHUNKS_PER_SEC))

                # Clamp to audit remaining if bounded test
                if audit_rem > 0:
                    chunk_size = min(chunk_size, audit_rem)

                end_idx = pool_idx + chunk_size
                if end_idx <= pool_len:
                    chunk = pool[pool_idx:end_idx]
                else:
                    chunk = pool[pool_idx:] + pool[:end_idx - pool_len]
                pool_idx = end_idx % pool_len

                await event_queue.put(chunk)
                _gen_stats["events_generated"] += chunk_size
                epoch_count += chunk_size

                # Bounded test accounting
                if audit_rem > 0:
                    runtime_config["audit_target_remaining"] -= chunk_size
                    if runtime_config["audit_target_remaining"] <= 0:
                        runtime_config["audit_target_remaining"] = 0
                        runtime_config["is_running"] = False
                        print("[generator] Audit target reached -- auto-paused")

            # --- Token-bucket timing ---
            elapsed = time.monotonic() - epoch_start
            expected = epoch_count / target if target > 0 else 0

            if epoch_count >= target:
                remaining = 1.0 - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
                epoch_start = time.monotonic()
                epoch_count = 0
            elif expected > elapsed:
                await asyncio.sleep(expected - elapsed)
            elif source == "random" and epoch_count % 30 == 0:
                await asyncio.sleep(0)
            elif source == "static":
                await asyncio.sleep(0)

            if time.monotonic() - epoch_start >= 1.0:
                epoch_start = time.monotonic()
                epoch_count = 0

    except asyncio.CancelledError:
        _gen_stats["generator_running"] = False
        print("[generator] Stopped.")


# ---------------------------------------------------------------------------
# Phase 3: Concurrent Consumer Workers
# ---------------------------------------------------------------------------

async def consume_events(worker_id: int) -> None:
    """
    Pull items from the queue -- handles both modes:
    - list[dict]  (static mode chunks)
    - dict        (random mode single events)

    Aggregates metrics locally, then flushes once under the lock.
    Uses round(,2) throughout to prevent floating-point drift.
    """
    print(f"[worker-{worker_id}] Consumer worker started.")

    try:
        while True:
            item = await event_queue.get()

            # -- Normalise: always work with a list of dicts --
            if isinstance(item, list):
                chunk = item
            else:
                chunk = [item]

            # -- Local accumulators (pure Python, no async overhead) --
            local_events = len(chunk)
            local_revenue = 0.0
            local_cat: dict[str, float] = {}
            local_reg: dict[str, float] = {}

            for evt in chunk:
                rev = round(evt["price"] * evt["quantity"], 2)
                local_revenue = round(local_revenue + rev, 2)
                cat = evt["category"]
                reg = evt["region"]
                local_cat[cat] = round(local_cat.get(cat, 0.0) + rev, 2)
                local_reg[reg] = round(local_reg.get(reg, 0.0) + rev, 2)

            # Phase 5: optional heavy computation toggle (non-blocking)
            if runtime_config["heavy_computation"]:
                await asyncio.sleep(0.001)

            # -- Single lock acquire to flush --
            async with state_lock:
                shared_state["total_events_processed"] += local_events
                shared_state["total_revenue"] = round(
                    shared_state["total_revenue"] + local_revenue, 2
                )
                for cat, rev in local_cat.items():
                    shared_state["revenue_by_category"][cat] = round(
                        shared_state["revenue_by_category"][cat] + rev, 2
                    )
                for reg, rev in local_reg.items():
                    shared_state["revenue_by_region"][reg] = round(
                        shared_state["revenue_by_region"][reg] + rev, 2
                    )

            event_queue.task_done()

    except asyncio.CancelledError:
        print(f"[worker-{worker_id}] Stopped.")


# ---------------------------------------------------------------------------
# Worker pool helpers (Phase 5)
# ---------------------------------------------------------------------------

def _spawn_worker() -> int:
    """Create a new consumer worker task and return its ID."""
    global _next_worker_id
    wid = _next_worker_id
    _next_worker_id += 1
    task = asyncio.create_task(consume_events(wid), name=f"worker-{wid}")
    _worker_tasks[wid] = task
    _background_tasks.append(task)
    return wid


async def _remove_worker(wid: int) -> None:
    """Cancel a specific worker task by ID."""
    task = _worker_tasks.pop(wid, None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if task in _background_tasks:
        _background_tasks.remove(task)


# ---------------------------------------------------------------------------
# Phase 4: WebSocket Connection Manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self):
        self._active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._active.add(ws)
        print(f"[ws] Client connected — {len(self._active)} active")

    def disconnect(self, ws: WebSocket):
        self._active.discard(ws)
        print(f"[ws] Client disconnected — {len(self._active)} active")

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def broadcast(self, message: str):
        """Send a message to every connected client, removing dead ones."""
        dead: list[WebSocket] = []
        for ws in self._active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._active.discard(ws)


ws_manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Phase 4: Broadcast Loop
# ---------------------------------------------------------------------------

async def broadcast_metrics() -> None:
    """
    Every BROADCAST_INTERVAL seconds, snapshot the shared state and
    broadcast it (plus system metrics) to all WebSocket clients.
    Includes server_timestamp for client-side latency measurement.
    Uses sliding-window rate trackers for smooth EPS readings.
    """
    print(f"[broadcast] Starting broadcast loop -- interval: {BROADCAST_INTERVAL}s")

    try:
        while True:
            await asyncio.sleep(BROADCAST_INTERVAL)

            now = time.time()

            # --- Snapshot shared state under lock ----------------------------
            async with state_lock:
                current_processed = shared_state["total_events_processed"]
                snapshot = {
                    k: (dict(v) if isinstance(v, dict) else v)
                    for k, v in shared_state.items()
                }

            # --- Sliding-window rates (smooth, no spikes) --------------------
            processed_eps = _processed_rate_tracker.push(now, current_processed)
            generated_eps = _generated_rate_tracker.push(now, _gen_stats["events_generated"])

            gen_elapsed = now - _gen_stats["gen_start_time"] if _gen_stats["gen_start_time"] else 0

            # --- Build broadcast payload -------------------------------------
            payload = {
                **snapshot,
                "server_timestamp": now,
                "system": {
                    "queue_backlog_size": event_queue.qsize(),
                    "events_generated_per_sec": generated_eps,
                    "events_processed_per_sec": processed_eps,
                    "active_ws_clients": ws_manager.active_count,
                    "active_workers": len(_worker_tasks),
                    "target_eps": runtime_config["target_eps"],
                    "heavy_computation": runtime_config["heavy_computation"],
                    "data_source": runtime_config["data_source"],
                    "is_running": runtime_config["is_running"],
                    "audit_target_remaining": runtime_config["audit_target_remaining"],
                    "server_uptime_sec": round(gen_elapsed, 1),
                },
            }

            if ws_manager.active_count > 0:
                await ws_manager.broadcast(json.dumps(payload))

    except asyncio.CancelledError:
        print("[broadcast] Stopped.")


# ---------------------------------------------------------------------------
# FastAPI lifespan — start / stop background tasks
# ---------------------------------------------------------------------------
_background_tasks: list[asyncio.Task] = []

DEFAULT_NUM_WORKERS = 6


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _next_worker_id, _STATIC_POOL, _STATIC_BASELINE

    # --- Load static test data (if available) ---
    test_data_path = os.path.join(os.path.dirname(__file__) or ".", "test_data.json")
    if os.path.isfile(test_data_path):
        try:
            with open(test_data_path, "r", encoding="utf-8") as f:
                test_data = json.load(f)
            _STATIC_POOL = test_data.get("events", [])
            _STATIC_BASELINE = test_data.get("baseline", {})
            print(f"[lifespan] Loaded static test data: {len(_STATIC_POOL)} events")
        except Exception as e:
            print(f"[lifespan] WARNING: Could not load test_data.json: {e}")
    else:
        print(f"[lifespan] WARNING: test_data.json not found -- static mode will use random pool")

    # --- STARTUP ---
    # Phase 2: Event generator
    gen_task = asyncio.create_task(generate_events(), name="event-generator")
    _background_tasks.append(gen_task)

    # Phase 3: Consumer workers
    for _ in range(DEFAULT_NUM_WORKERS):
        _spawn_worker()

    # Phase 4: WebSocket broadcast loop
    bcast_task = asyncio.create_task(broadcast_metrics(), name="broadcast-loop")
    _background_tasks.append(bcast_task)

    print(f"[lifespan] Started: 1 generator, {DEFAULT_NUM_WORKERS} workers, 1 broadcast loop")
    yield

    # --- SHUTDOWN ---
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    print("[lifespan] All background tasks cancelled.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="E-Commerce Telemetry Stream",
    description="Real-time event processing backend simulation",
    version="1.0.0",
    lifespan=lifespan,
)

# Phase 7: CORS — allow frontend to call backend from file:// or any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Phase 7: Serve the frontend dashboard as static files
app.mount("/dashboard", StaticFiles(directory="frontend", html=True), name="dashboard")


# ---------------------------------------------------------------------------
# Phase 5: Request models for control endpoints
# ---------------------------------------------------------------------------

class RateConfig(BaseModel):
    target_eps: int = Field(..., ge=100, le=10_000_000, description="Target events per second (100-10,000,000)")

class WorkerConfig(BaseModel):
    worker_count: int = Field(..., ge=1, le=20, description="Desired number of active workers (1–20)")

class MetricsConfig(BaseModel):
    heavy_computation: bool = Field(..., description="Enable/disable simulated heavy computation in workers")

class SourceConfig(BaseModel):
    source: str = Field(..., pattern="^(random|static)$", description="Data source mode: 'random' or 'static'")

class StateConfig(BaseModel):
    is_running: bool = Field(..., description="Pause (false) or resume (true) the event generator")

class AuditConfig(BaseModel):
    total_events: int = Field(..., ge=1, le=100_000_000, description="Total events to generate in the audit run")
    target_eps: int = Field(..., ge=100, le=10_000_000, description="Target events per second for the audit run")


# ---------------------------------------------------------------------------
# Phase 5: Dynamic System Control Endpoints
# ---------------------------------------------------------------------------

@app.post("/config/rate", tags=["config"])
async def set_event_rate(cfg: RateConfig):
    """Adjust the event generator's target rate on the fly."""
    old = runtime_config["target_eps"]
    runtime_config["target_eps"] = cfg.target_eps
    print(f"[config] Event rate changed: {old} -> {cfg.target_eps} eps")
    return {
        "status": "ok",
        "previous_target_eps": old,
        "new_target_eps": cfg.target_eps,
    }


@app.post("/config/workers", tags=["config"])
async def set_worker_count(cfg: WorkerConfig):
    """
    Scale the worker pool up or down.
    - If new count > current: spawn additional workers.
    - If new count < current: cancel excess workers (LIFO order).
    """
    current = len(_worker_tasks)
    target = cfg.worker_count

    spawned = []
    removed = []

    if target > current:
        # Spawn new workers
        for _ in range(target - current):
            wid = _spawn_worker()
            spawned.append(wid)
    elif target < current:
        # Cancel excess workers (remove highest IDs first)
        ids_to_remove = sorted(_worker_tasks.keys(), reverse=True)[: current - target]
        for wid in ids_to_remove:
            await _remove_worker(wid)
            removed.append(wid)

    print(f"[config] Workers: {current} -> {len(_worker_tasks)} (spawned={spawned}, removed={removed})")
    return {
        "status": "ok",
        "previous_worker_count": current,
        "new_worker_count": len(_worker_tasks),
        "spawned_ids": spawned,
        "removed_ids": removed,
    }


@app.post("/config/metrics", tags=["config"])
async def set_metrics_toggle(cfg: MetricsConfig):
    """Toggle heavy computation mode in consumer workers."""
    old = runtime_config["heavy_computation"]
    runtime_config["heavy_computation"] = cfg.heavy_computation
    label = "ENABLED" if cfg.heavy_computation else "DISABLED"
    print(f"[config] Heavy computation {label}")
    return {
        "status": "ok",
        "previous_heavy_computation": old,
        "new_heavy_computation": cfg.heavy_computation,
    }


@app.post("/config/source", tags=["config"])
async def set_data_source(cfg: SourceConfig):
    """Toggle between 'random' (chaos testing) and 'static' (deterministic auditing)."""
    old = runtime_config["data_source"]
    runtime_config["data_source"] = cfg.source
    pool_status = f"{len(_STATIC_POOL)} events" if _STATIC_POOL else "using random fallback pool"
    print(f"[config] Data source changed: {old} -> {cfg.source} (static pool: {pool_status})")
    return {
        "status": "ok",
        "previous_source": old,
        "new_source": cfg.source,
        "static_pool_loaded": len(_STATIC_POOL) > 0,
        "static_pool_size": len(_STATIC_POOL),
    }


@app.post("/config/state", tags=["config"])
async def set_generator_state(cfg: StateConfig):
    """Pause or resume the event generator."""
    old = runtime_config["is_running"]
    runtime_config["is_running"] = cfg.is_running
    label = "RESUMED" if cfg.is_running else "PAUSED"
    print(f"[config] Generator {label}")
    return {"status": "ok", "previous_is_running": old, "new_is_running": cfg.is_running}


@app.post("/config/reset", tags=["config"])
async def reset_metrics():
    """Reset all shared metrics, flush the queue, and reset gen stats."""
    runtime_config["is_running"] = False
    await asyncio.sleep(0.3)  # let generator pause

    # Flush the queue
    flushed = 0
    while not event_queue.empty():
        try:
            event_queue.get_nowait()
            event_queue.task_done()
            flushed += 1
        except asyncio.QueueEmpty:
            break

    # Reset shared state
    async with state_lock:
        shared_state["total_events_processed"] = 0
        shared_state["total_revenue"] = 0.0
        for cat in CATEGORIES:
            shared_state["revenue_by_category"][cat] = 0.0
        for reg in REGIONS:
            shared_state["revenue_by_region"][reg] = 0.0

    # Reset gen stats
    _gen_stats["events_generated"] = 0
    _gen_stats["gen_start_time"] = time.time()

    # Reset rate trackers
    _processed_rate_tracker.clear()
    _generated_rate_tracker.clear()

    runtime_config["audit_target_remaining"] = 0

    print(f"[config] Metrics reset (flushed {flushed} queue items)")
    return {"status": "ok", "flushed_queue_items": flushed}


@app.post("/audit/run", tags=["audit"])
async def start_audit_run(cfg: AuditConfig):
    """
    Orchestrated bounded audit test.
    Resets state, switches to static mode, sets target, and starts.
    """
    # 1. Pause and reset
    runtime_config["is_running"] = False
    await asyncio.sleep(0.3)

    # Flush queue
    flushed = 0
    while not event_queue.empty():
        try:
            event_queue.get_nowait()
            event_queue.task_done()
            flushed += 1
        except asyncio.QueueEmpty:
            break

    # Reset shared state
    async with state_lock:
        shared_state["total_events_processed"] = 0
        shared_state["total_revenue"] = 0.0
        for cat in CATEGORIES:
            shared_state["revenue_by_category"][cat] = 0.0
        for reg in REGIONS:
            shared_state["revenue_by_region"][reg] = 0.0

    # Reset gen stats
    _gen_stats["events_generated"] = 0
    _gen_stats["gen_start_time"] = time.time()

    _processed_rate_tracker.clear()
    _generated_rate_tracker.clear()

    # 2. Configure for audit
    runtime_config["data_source"] = "static"
    runtime_config["target_eps"] = cfg.target_eps
    runtime_config["audit_target_remaining"] = cfg.total_events

    # 3. Start
    runtime_config["is_running"] = True

    print(f"[audit] Started: {cfg.total_events:,} events @ {cfg.target_eps:,} eps (static mode)")
    return {
        "status": "ok",
        "total_events": cfg.total_events,
        "target_eps": cfg.target_eps,
        "data_source": "static",
        "flushed_queue_items": flushed,
    }


# ---------------------------------------------------------------------------
# Phase 4: WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws/metrics")
async def websocket_metrics(ws: WebSocket):
    """
    Clients connect here to receive live metric broadcasts.
    The actual broadcasting is done by the broadcast_metrics() loop;
    this endpoint just manages the connection lifecycle.
    """
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Debug / verification endpoints
# ---------------------------------------------------------------------------
@app.get("/debug/queue", tags=["debug"])
async def debug_queue_status():
    """Queue size, total events generated, and effective generation rate."""
    elapsed = time.time() - _gen_stats["gen_start_time"] if _gen_stats["gen_start_time"] else 0
    effective_rate = _gen_stats["events_generated"] / elapsed if elapsed > 0 else 0

    return JSONResponse(
        content={
            "queue_size": event_queue.qsize(),
            "total_events_generated": _gen_stats["events_generated"],
            "generator_running": _gen_stats["generator_running"],
            "elapsed_seconds": round(elapsed, 2),
            "effective_events_per_second": round(effective_rate, 1),
        }
    )


@app.get("/debug/state", tags=["debug"])
async def debug_shared_state():
    """Return the current shared state (metrics). Safe read under the lock."""
    async with state_lock:
        snapshot = {k: v if not isinstance(v, dict) else dict(v) for k, v in shared_state.items()}
    return JSONResponse(content=snapshot)


@app.get("/debug/config", tags=["debug"])
async def debug_config():
    """Return the current runtime configuration and worker IDs."""
    return {
        "runtime_config": runtime_config,
        "active_worker_ids": sorted(_worker_tasks.keys()),
        "active_worker_count": len(_worker_tasks),
    }


@app.get("/debug/baseline", tags=["debug"])
async def debug_baseline():
    """Return the static test data baseline (from test_data.json)."""
    if not _STATIC_BASELINE:
        return JSONResponse(
            status_code=404,
            content={"error": "No baseline loaded. Run 'python generate_test_data.py' first."},
        )
    return JSONResponse(content=_STATIC_BASELINE)


@app.get("/health", tags=["system"])
async def health_check():
    return {"status": "ok"}
