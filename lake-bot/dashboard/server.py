"""
server.py — FastAPI dashboard server for Lake Bot.

Runs on port 8080 (laptop-side).
  • Serves static frontend from ./static/
  • Opens a WebSocket client to ws://<PI_HOST>:8765 and relays
    telemetry to all browser clients via /ws.
  • Relays commands from browser clients back to the Pi.
  • Retries Pi connection every 3 seconds on disconnect;
    pushes {"type": "pi_connection", "status": "offline"} to browsers
    while disconnected.

Usage:
  python3 server.py --pi-host 192.168.1.42
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Set

import uvicorn
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dashboard")


# ---------------------------------------------------------------------------
# Argument parsing (done at module level so uvicorn can import cleanly)
# ---------------------------------------------------------------------------

_parser = argparse.ArgumentParser(description="Lake Bot Dashboard Server")
_parser.add_argument(
    "--pi-host",
    required=True,
    metavar="HOST",
    help="IP address or hostname of the Raspberry Pi running nav_service",
)
_parser.add_argument(
    "--port",
    type=int,
    default=8080,
    help="Port to run the dashboard server on",
)
_args, _unknown = _parser.parse_known_args()

PI_HOST: str = _args.pi_host
PI_WS_URL: str = f"ws://{PI_HOST}:8765"
DASHBOARD_PORT: int = _args.port


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Lake Bot Dashboard")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# Browser WebSocket manager
# ---------------------------------------------------------------------------

class BrowserConnectionManager:
    def __init__(self):
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        log.info(f"Browser client connected. Total: {len(self._connections)}")

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        log.info(f"Browser client disconnected. Total: {len(self._connections)}")

    async def broadcast(self, message: str) -> None:
        dead: Set[WebSocket] = set()
        for ws in list(self._connections):
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        self._connections -= dead


manager = BrowserConnectionManager()

# Queue for commands coming from browser → Pi
_pi_send_queue: asyncio.Queue[str] = asyncio.Queue()

# Pi connection status
_pi_online: bool = False


@app.websocket("/ws")
async def browser_ws(ws: WebSocket) -> None:
    """Browser ↔ Dashboard relay endpoint."""
    await manager.connect(ws)
    # Immediately inform the new client of Pi connection status
    status = "online" if _pi_online else "offline"
    await ws.send_text(json.dumps({"type": "pi_connection", "status": status}))
    try:
        while True:
            data = await ws.receive_text()
            # Forward command to Pi via the send queue
            await _pi_send_queue.put(data)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Pi relay task
# ---------------------------------------------------------------------------

async def _pi_relay_loop() -> None:
    """
    Maintain a persistent WebSocket connection to the Pi.
    Retries every 3 seconds on failure.
    Relays telemetry → browsers and browser commands → Pi.
    """
    global _pi_online

    while True:
        log.info(f"Connecting to Pi at {PI_WS_URL} …")
        try:
            async with websockets.connect(
                PI_WS_URL,
                ping_interval=10,
                ping_timeout=20,
            ) as pi_ws:
                _pi_online = True
                await manager.broadcast(
                    json.dumps({"type": "pi_connection", "status": "online"})
                )
                log.info("Connected to Pi WebSocket.")

                # Run receive and send concurrently
                receive_task = asyncio.create_task(_receive_from_pi(pi_ws))
                send_task = asyncio.create_task(_send_to_pi(pi_ws))

                done, pending = await asyncio.wait(
                    {receive_task, send_task},
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for t in pending:
                    t.cancel()
                # Surface exception if any
                for t in done:
                    if t.exception():
                        log.warning(f"Pi relay task ended: {t.exception()}")

        except (websockets.exceptions.WebSocketException, OSError, ConnectionRefusedError) as exc:
            log.warning(f"Pi connection failed: {exc}")

        # Connection lost
        _pi_online = False
        await manager.broadcast(
            json.dumps({"type": "pi_connection", "status": "offline"})
        )
        log.info("Pi offline. Retrying in 3 seconds …")
        await asyncio.sleep(3)


async def _receive_from_pi(pi_ws) -> None:
    """Forward every message from the Pi to all browser clients."""
    async for message in pi_ws:
        await manager.broadcast(message)


async def _send_to_pi(pi_ws) -> None:
    """Forward queued commands from browsers to the Pi."""
    while True:
        command = await _pi_send_queue.get()
        await pi_ws.send(command)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_pi_relay_loop())
    log.info(f"Dashboard server started on port {DASHBOARD_PORT}. Pi target: {PI_WS_URL}")


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=DASHBOARD_PORT,
        log_level="info",
    )
