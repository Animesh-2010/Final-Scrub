"""
telemetry_server.py — WebSocket telemetry server for Lake Bot nav_service.

Listens on 0.0.0.0:8765.
  • Broadcasts telemetry JSON at 3 Hz to all connected clients.
  • Accepts command JSON from any connected client and dispatches to
    the NavigationController.
  • Unknown or malformed messages get a {"type": "error", "message": "..."} reply.
  • Never crashes on bad input.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

from navigation import NavigationController, NavState
from logger import NavLogger


log = logging.getLogger(__name__)


class TelemetryServer:
    """
    Async WebSocket server that broadcasts nav telemetry and handles commands.

    Parameters
    ----------
    nav_ctrl    : NavigationController instance
    logger      : NavLogger instance
    host        : bind address (default 0.0.0.0)
    port        : bind port (default 8765)
    broadcast_rate_hz : telemetry ticks per second (default 3)
    """

    def __init__(
        self,
        nav_ctrl: NavigationController,
        logger: NavLogger,
        host: str = "0.0.0.0",
        port: int = 8765,
        broadcast_rate_hz: float = 3.0,
    ):
        self._nav = nav_ctrl
        self._logger = logger
        self._host = host
        self._port = port
        self._tick_interval = 1.0 / broadcast_rate_hz
        self._clients: Set[WebSocketServerProtocol] = set()
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Telemetry message builder
    # ------------------------------------------------------------------

    def _build_telemetry(self) -> dict:
        nav = self._nav
        fix = nav.last_fix

        return {
            "type": "telemetry",
            "lat": fix.lat if fix else 0.0,
            "lon": fix.lon if fix else 0.0,
            "heading_deg": nav.heading,
            "speed_mps": fix.speed_mps if fix else 0.0,
            "state": nav.state.value,
            "current_waypoint_index": nav.current_waypoint_index,
            "satellites": fix.satellites if fix else 0,
            "fix_quality": fix.fix_quality if fix else 0,
            "left_power": nav.left_power,
            "right_power": nav.right_power,
            "timestamp": time.time(),
        }

    # ------------------------------------------------------------------
    # Command dispatcher
    # ------------------------------------------------------------------

    async def _dispatch_command(
        self,
        ws: WebSocketServerProtocol,
        msg: dict,
    ) -> None:
        """Route an inbound command dict to the NavigationController."""
        nav = self._nav
        cmd_type = msg.get("type", "")

        self._logger.log_event("command_received", json.dumps(msg))

        if cmd_type == "load_mission":
            waypoints = msg.get("waypoints")
            if not isinstance(waypoints, list):
                await self._send_error(ws, "load_mission requires 'waypoints' list")
                return
            nav.load_mission(waypoints)

        elif cmd_type == "set_gcs":
            lat = msg.get("lat")
            lon = msg.get("lon")
            if lat is None or lon is None:
                await self._send_error(ws, "set_gcs requires 'lat' and 'lon'")
                return
            nav.gcs_lat = float(lat)
            nav.gcs_lon = float(lon)

        elif cmd_type == "start":
            nav.cmd_start()

        elif cmd_type == "pause":
            nav.cmd_pause()

        elif cmd_type == "stop":
            nav.cmd_stop()

        elif cmd_type == "set_speed":
            power = msg.get("power")
            if power is None:
                await self._send_error(ws, "set_speed requires 'power'")
                return
            nav.set_cruise_power(int(power))

        elif cmd_type == "emergency_stop":
            nav.cmd_emergency_stop()

        else:
            await self._send_error(ws, f"Unknown command type: '{cmd_type}'")

    async def _send_error(self, ws: WebSocketServerProtocol, reason: str) -> None:
        try:
            await ws.send(json.dumps({"type": "error", "message": reason}))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_client(self, ws: WebSocketServerProtocol) -> None:
        self._clients.add(ws)
        log.info(f"Client connected: {ws.remote_address}")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        await self._send_error(ws, "Message must be a JSON object")
                        continue
                    await self._dispatch_command(ws, msg)
                except json.JSONDecodeError as exc:
                    await self._send_error(ws, f"Malformed JSON: {exc}")
                except Exception as exc:
                    log.exception(f"Error processing command: {exc}")
                    await self._send_error(ws, f"Internal error: {exc}")
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            is_last_client = len(self._clients) <= 1
            self._clients.discard(ws)
            if is_last_client and self._nav.state in (NavState.RUNNING, NavState.PAUSED):
                log.warning("Last client disconnected — fail-safe stopping motors")
                self._nav.cmd_stop()
            log.info(f"Client disconnected: {ws.remote_address}")

    # ------------------------------------------------------------------
    # Broadcast loop
    # ------------------------------------------------------------------

    async def _broadcast_loop(self) -> None:
        """Run nav tick + broadcast telemetry at the configured rate."""
        while not self._stop_event.is_set():
            start = time.monotonic()

            # Navigation tick
            try:
                self._nav.tick()
            except Exception as exc:
                log.exception(f"Navigation tick error: {exc}")

            # Build and broadcast telemetry
            telem = self._build_telemetry()
            telem_json = json.dumps(telem)

            # Log to SQLite
            try:
                self._logger.log_telemetry(
                    lat=telem["lat"],
                    lon=telem["lon"],
                    heading_deg=telem["heading_deg"],
                    speed_mps=telem["speed_mps"],
                    state=telem["state"],
                    waypoint_index=telem["current_waypoint_index"],
                    left_power=telem["left_power"],
                    right_power=telem["right_power"],
                    timestamp=telem["timestamp"],
                )
            except Exception as exc:
                log.warning(f"Logger write error: {exc}")

            # Send to all connected clients
            if self._clients:
                dead = set()
                for client in self._clients:
                    try:
                        await client.send(telem_json)
                    except Exception:
                        dead.add(client)
                self._clients -= dead

            # Sleep for remainder of interval
            elapsed = time.monotonic() - start
            sleep_time = max(0.0, self._tick_interval - elapsed)
            await asyncio.sleep(sleep_time)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def serve(self) -> None:
        """Start the WebSocket server and broadcast loop. Runs until stopped."""
        server = await websockets.serve(
            self._handle_client,
            self._host,
            self._port,
        )
        log.info(f"Telemetry WebSocket server listening on ws://{self._host}:{self._port}")

        broadcast_task = asyncio.create_task(self._broadcast_loop())
        try:
            await self._stop_event.wait()
        finally:
            broadcast_task.cancel()
            server.close()
            await server.wait_closed()

    def stop(self) -> None:
        """Signal the server to shut down gracefully."""
        self._stop_event.set()
