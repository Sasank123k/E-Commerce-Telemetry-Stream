"""
simulator.py — WebSocket Client Simulator for stress-testing the backend.

Phase 6: Spins up 200 concurrent WebSocket clients, measures latency
of each broadcast message, and prints a rolling summary every 3 seconds.

Usage:
    python simulator.py [--clients 200] [--url ws://127.0.0.1:8000/ws/metrics]
"""

import argparse
import asyncio
import json
import statistics
import time

import websockets

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
DEFAULT_URL = "ws://127.0.0.1:8000/ws/metrics"
DEFAULT_CLIENTS = 200
REPORT_INTERVAL = 3  # seconds between summary prints

# ---------------------------------------------------------------------------
# Shared latency collection
# ---------------------------------------------------------------------------
_lock = asyncio.Lock()
_latencies: list[float] = []
_active_connections = 0


async def _record_latency(latency_ms: float):
    global _latencies
    async with _lock:
        _latencies.append(latency_ms)


async def _inc_connections(delta: int):
    global _active_connections
    async with _lock:
        _active_connections += delta


async def _drain_latencies() -> list[float]:
    """Atomically drain and return all collected latencies."""
    global _latencies
    async with _lock:
        batch = _latencies[:]
        _latencies = []
        return batch


async def _get_active() -> int:
    async with _lock:
        return _active_connections


# ---------------------------------------------------------------------------
# Single WebSocket client
# ---------------------------------------------------------------------------

async def ws_client(client_id: int, url: str):
    """Connect to the server and continuously measure latency."""
    retry_delay = 1

    while True:
        try:
            async with websockets.connect(url) as ws:
                await _inc_connections(1)
                retry_delay = 1  # reset on successful connect

                async for raw_msg in ws:
                    receipt_time = time.time()
                    try:
                        data = json.loads(raw_msg)
                        server_ts = data.get("server_timestamp")
                        if server_ts:
                            latency_ms = (receipt_time - server_ts) * 1000
                            await _record_latency(latency_ms)
                    except (json.JSONDecodeError, TypeError):
                        pass

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as exc:
            pass  # will reconnect
        finally:
            await _inc_connections(-1)

        # Exponential back-off on reconnect (cap at 5s)
        await asyncio.sleep(min(retry_delay, 5))
        retry_delay *= 2


# ---------------------------------------------------------------------------
# Rolling summary reporter
# ---------------------------------------------------------------------------

async def reporter():
    """Print a latency summary every REPORT_INTERVAL seconds."""
    print(f"\n{'='*65}")
    print(f"  E-Commerce Telemetry Stream — Simulator")
    print(f"{'='*65}\n")

    while True:
        await asyncio.sleep(REPORT_INTERVAL)

        batch = await _drain_latencies()
        active = await _get_active()

        if batch:
            avg = statistics.mean(batch)
            med = statistics.median(batch)
            mx  = max(batch)
            mn  = min(batch)
            p95 = sorted(batch)[int(len(batch) * 0.95)] if len(batch) >= 20 else mx

            print(
                f"[sim] Connections: {active:>4} | "
                f"Msgs: {len(batch):>6} | "
                f"Latency  avg: {avg:>7.1f}ms  med: {med:>7.1f}ms  "
                f"p95: {p95:>7.1f}ms  max: {mx:>7.1f}ms  min: {mn:>7.1f}ms"
            )
        else:
            print(f"[sim] Connections: {active:>4} | No messages received yet...")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def main(url: str, num_clients: int):
    print(f"[sim] Launching {num_clients} clients → {url}")
    print(f"[sim] Reporting every {REPORT_INTERVAL}s\n")

    # Start the reporter
    asyncio.create_task(reporter())

    # Launch all clients concurrently
    tasks = [asyncio.create_task(ws_client(i, url)) for i in range(num_clients)]

    # Run forever (Ctrl+C to stop)
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WebSocket client simulator")
    parser.add_argument("--clients", type=int, default=DEFAULT_CLIENTS, help=f"Number of concurrent clients (default: {DEFAULT_CLIENTS})")
    parser.add_argument("--url", type=str, default=DEFAULT_URL, help=f"WebSocket URL (default: {DEFAULT_URL})")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.url, args.clients))
    except KeyboardInterrupt:
        print("\n[sim] Shutting down.")
