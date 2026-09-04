"""
server.py — FastAPI app for SCRUB v4 nav_service.

Runs directly on the Raspberry Pi. No laptop-side relay.

Endpoints:
  GET  /api/health          — health check
  WS   /ws                  — telemetry broadcast + command dispatch
  GET  /api/missions        — list past missions
  GET  /api/missions/{id}/waypoints        — averaged results per waypoint
  GET  /api/missions/{id}/dwell_samples?waypoint_index=N — raw samples
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Module-level shared state — populated by create_app(), read by
# broadcast_loop / broadcast_dwell_sample / main.py
# ------------------------------------------------------------------

_state: dict = {}


def create_app(
    nav_ctrl,
    logger,
    firebase_sync=None,
    broadcast_hz: float = 3.0,
    dashboard_dir: Optional[str] = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    global _state
    app = FastAPI(title="SCRUB v4 Nav Service")
    _state = {
        "nav": nav_ctrl,
        "logger": logger,
        "firebase": firebase_sync,
        "broadcast_interval": 1.0 / broadcast_hz,
        "clients": set(),
        "stop_event": asyncio.Event(),
    }

    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def health():
        nav = _state["nav"]
        uptime = time.monotonic()
        return {
            "status": "ok",
            "arduino_link": "connected",
            "uptime_s": round(uptime, 1),
        }

    @app.get("/api/missions")
    async def list_missions():
        return _state["logger"].get_missions()

    @app.get("/api/missions/{mission_id}/waypoints")
    async def mission_waypoints(mission_id: int):
        return _state["logger"].get_waypoint_results(mission_id)

    @app.get("/api/missions/{mission_id}/dwell_samples")
    async def mission_dwell_samples(
        mission_id: int,
        waypoint_index: Optional[int] = Query(None),
    ):
        return _state["logger"].get_dwell_samples(mission_id, waypoint_index)

    # ------------------------------------------------------------------
    # WebSocket telemetry + command dispatch
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        _state["clients"].add(ws)

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        await _send_error(ws, "Message must be a JSON object")
                        continue
                    await _dispatch_command(ws, msg, _state)
                except json.JSONDecodeError as exc:
                    await _send_error(ws, f"Malformed JSON: {exc}")
                except Exception as exc:
                    log.exception(f"Error processing command: {exc}")
                    await _send_error(ws, f"Internal error: {exc}")
        except WebSocketDisconnect:
            pass
        finally:
            _state["clients"].discard(ws)
            nav = _state["nav"]
            if len(_state["clients"]) == 0 and nav.state.value in ("RUNNING", "PAUSED"):
                log.warning("Last WS client disconnected — pausing mission")
                nav.cmd_stop()

    # ------------------------------------------------------------------
    # Local dashboard (served by the Pi itself over WiFi).
    # ------------------------------------------------------------------

    if dashboard_dir:
        import os

        dash = os.path.abspath(dashboard_dir)
        index_path = os.path.join(dash, "index.html")
        if os.path.isdir(dash):
            app.mount(
                "/static",
                StaticFiles(directory=dash),
                name="dashboard-static",
            )

            @app.get("/", include_in_schema=False)
            async def dashboard_root():
                return FileResponse(index_path)

            log.info("Serving local dashboard from %s at http://<pi-ip>:8000/", dash)
        else:
            log.warning("dashboard_dir '%s' not found — skipping static dashboard", dash)

    return app


# ------------------------------------------------------------------
# Command dispatcher (ported from lake-bot telemetry_server.py)
# ------------------------------------------------------------------

async def _dispatch_command(ws: WebSocket, msg: dict, state: dict) -> None:
    """Route an inbound command dict to the NavigationController."""
    nav = state["nav"]
    logger = state["logger"]
    cmd_type = msg.get("type", "")
    _cmd_start = time.monotonic()

    logger.log_event("command_received", json.dumps(msg))

    if cmd_type == "load_mission":
        waypoints = msg.get("waypoints")
        if not isinstance(waypoints, list):
            await _send_error(ws, "load_mission requires 'waypoints' list")
            return
        nav.load_mission(waypoints)

    elif cmd_type == "set_gcs":
        lat = msg.get("lat")
        lon = msg.get("lon")
        if lat is None or lon is None:
            await _send_error(ws, "set_gcs requires 'lat' and 'lon'")
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
            await _send_error(ws, "set_speed requires 'power'")
            return
        nav.set_cruise_power(int(power))

    elif cmd_type == "emergency_stop":
        nav.cmd_emergency_stop()

    elif cmd_type == "set_manual":
        enabled = msg.get("enabled")
        if enabled is None:
            await _send_error(ws, "set_manual requires 'enabled' (bool)")
            return
        nav.set_dashboard_override(bool(enabled))

    elif cmd_type == "get_history":
        mission_id = msg.get("mission_id")
        if mission_id is not None:
            results = logger.get_waypoint_results(int(mission_id))
            await ws.send_json({"type": "history", "mission_id": mission_id, "results": results})
        else:
            missions = logger.get_missions()
            await ws.send_json({"type": "history", "missions": missions})

    else:
        await _send_error(ws, f"Unknown command type: '{cmd_type}'")


async def _send_error(ws: WebSocket, reason: str) -> None:
    try:
        await ws.send_json({"type": "error", "message": reason})
    except Exception:
        pass


# ------------------------------------------------------------------
# Dwell sample broadcast
# ------------------------------------------------------------------

async def broadcast_dwell_sample(state: dict, dwell_sample: dict) -> None:
    """
    Broadcast a dwell_sample message to all connected WS clients.
    Called from the nav decision task when a sample is collected during DWELL state.
    """
    telem_json = json.dumps(dwell_sample)
    dead = set()
    for client in state["clients"]:
        try:
            await client.send_text(telem_json)
        except Exception:
            dead.add(client)
    state["clients"] -= dead


# ------------------------------------------------------------------
# Telemetry builder
# ------------------------------------------------------------------

def build_telemetry(nav_ctrl) -> dict:
    state = nav_ctrl._latest_state
    return {
        "type": "telemetry",
        "mission_id": getattr(nav_ctrl, "mission_id", None),
        "lat": state.gps_lat,
        "lon": state.gps_lon,
        "heading_deg": nav_ctrl.heading,
        "speed_mps": state.gps_spd,
        "state": nav_ctrl.state.value,
        "effective_mode": nav_ctrl._effective_mode(),
        "current_waypoint_index": nav_ctrl.current_waypoint_index,
        "total_waypoints": len(nav_ctrl.waypoints),
        "satellites": state.gps_sats,
        "sats_view": state.gps_sats_view,
        "sats_used": state.gps_sats_used,
        "fix_quality": state.gps_fix,
        "left_power": nav_ctrl.left_power,
        "right_power": nav_ctrl.right_power,
        "sensors": state.sensors,
        "compass": {"x": state.compass_x, "y": state.compass_y, "z": state.compass_z},
        "target_bearing": nav_ctrl.target_bearing,
        "heading_error": nav_ctrl.heading_error,
        "distance_to_target": nav_ctrl.distance_to_target,
        "motor_direction": getattr(nav_ctrl, "motor_direction", "S"),
        "motor_angle_deg": getattr(nav_ctrl, "motor_angle_deg", 0.0),
        "motor_rpm": getattr(nav_ctrl, "motor_rpm", 0.0),
        "timestamp": time.time(),
    }


# ------------------------------------------------------------------
# Broadcast task (runs as a background asyncio task)
# ------------------------------------------------------------------

async def broadcast_loop(state: dict) -> None:
    """
    Read current nav state and broadcast to all WS clients.
    Runs at the configured broadcast rate (default 3 Hz).
    """
    while not state["stop_event"].is_set():
        start = time.monotonic()

        telem = build_telemetry(state["nav"])
        _build_ms = (time.monotonic() - start) * 1000
        telem_json = json.dumps(telem)

        # Log to SQLite
        try:
            state["logger"].log_telemetry(
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

        # Push to Firebase (non-blocking)
        if state["firebase"]:
            state["firebase"].push_live_telemetry(telem)

        # Push to Supabase (non-blocking — enqueues to background thread)
        supabase_sync = state.get("supabase_sync")
        if supabase_sync and supabase_sync.enabled:
            supabase_sync.enqueue_telemetry(telem)

        # Send to all connected WS clients
        dead = set()
        for client in state["clients"]:
            try:
                await client.send_text(telem_json)
            except Exception:
                dead.add(client)
        state["clients"] -= dead

        elapsed = time.monotonic() - start
        sleep_time = max(0.0, state["broadcast_interval"] - elapsed)
        if _build_ms > 50:
            log.warning(f"Telemetry build took {_build_ms:.1f} ms (budget: 30 ms)")
        await asyncio.sleep(sleep_time)
