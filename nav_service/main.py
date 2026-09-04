"""
main.py — CLI entrypoint for SCRUB v4 nav_service.

Usage:
  # Dashboard-driven (waits for commands over WebSocket):
  python3 main.py --config config.yaml --simulate

  # Real hardware:
  python3 main.py --config config.yaml

  # CLI-only autostart (bench test):
  python3 main.py --config config.yaml --simulate \
      --waypoints waypoints_sample.json --autostart
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="SCRUB v4 Navigation Service",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p.add_argument("--simulate", action="store_true", help="Use SimulatedArduinoLink (no hardware)")
    p.add_argument("--waypoints", default=None, metavar="PATH", help="JSON waypoints file to preload")
    p.add_argument("--autostart", action="store_true", help="Immediately start the mission after loading waypoints")
    return p


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        log.warning(f"Config file not found: {path}, using defaults")
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Component factory
# ---------------------------------------------------------------------------

def build_components(args: argparse.Namespace, cfg: dict):
    from arduino_link import ArduinoLink, DualArduinoLink, SensorGpsLink, MotorRcLink, SimulatedArduinoLink
    from logger import NavLogger
    from navigation import NavigationController
    from firebase_sync import FirebaseSync
    from supabase_sync import SupabaseSync
    from server import create_app, broadcast_loop

    # Config sections
    sensor_gps_cfg = cfg.get("sensor_gps_link", {})
    motor_rc_cfg = cfg.get("motor_rc_link", {})
    gps_cfg = cfg.get("gps", {})
    nav_cfg = cfg.get("navigation", {})
    safety_cfg = cfg.get("safety", {})
    telem_cfg = cfg.get("telemetry", {})
    logger_cfg = cfg.get("logger", {})
    firebase_cfg = cfg.get("firebase", {})

    # Logger
    db_path = os.path.join(os.path.dirname(__file__), logger_cfg.get("db_path", "nav_log.db"))
    logger = NavLogger(db_path=db_path)

    # Arduino link — dual-board or legacy single-board
    if args.simulate:
        arduino = SimulatedArduinoLink()
    elif sensor_gps_cfg or motor_rc_cfg:
        # Dual-board configuration
        sensor_gps = SensorGpsLink(
            device=sensor_gps_cfg.get("device", "/dev/ttyAMA0"),
            baud=sensor_gps_cfg.get("baud", 115200),
            reconnect_backoff_s=sensor_gps_cfg.get("reconnect_backoff_s", 2.0),
            sensor_keys=sensor_gps_cfg.get("sensor_keys", ["ph", "tds", "turb", "wtemp", "atemp", "hum"]),
            staleness_timeout_s=sensor_gps_cfg.get("staleness_timeout_s", 3.0),
        )
        motor_rc = MotorRcLink(
            device=motor_rc_cfg.get("device", "/dev/ttyACM0"),
            baud=motor_rc_cfg.get("baud", 115200),
            reconnect_backoff_s=motor_rc_cfg.get("reconnect_backoff_s", 2.0),
            staleness_timeout_s=motor_rc_cfg.get("staleness_timeout_s", 1.0),
        )

        # Optional direct-to-Pi GPS (NMEA) — overrides Arduino GPS fields
        from gps_reader import GpsReader
        gps_reader = None
        if gps_cfg.get("enabled", False):
            gps_reader = GpsReader(
                device=gps_cfg.get("device", "/dev/ttyUSB1"),
                baud=gps_cfg.get("baud", 9600),
                reconnect_backoff_s=gps_cfg.get("reconnect_backoff_s", 2.0),
                staleness_timeout_s=gps_cfg.get("staleness_timeout_s", 5.0),
                backend=gps_cfg.get("backend", "pyserial"),
                picocom_cmd=gps_cfg.get("picocom_cmd", None),
            )

        arduino = DualArduinoLink(sensor_gps=sensor_gps, motor_rc=motor_rc, gps_reader=gps_reader)
    else:
        # Legacy single-board configuration (backward compatibility)
        arduino_cfg = cfg.get("arduino_link", {})
        arduino = ArduinoLink(
            device=arduino_cfg.get("device", "/dev/ttyAMA0"),
            baud=arduino_cfg.get("baud", 115200),
            reconnect_backoff_s=arduino_cfg.get("reconnect_backoff_s", 2.0),
        )
    arduino.open()

    # Navigation controller
    nav = NavigationController(
        arduino=arduino,
        logger=logger,
        arrival_radius_m=nav_cfg.get("waypoint_arrival_radius_m", 2.0),
        geofence_radius_m=safety_cfg.get("geofence_radius_m", 150.0),
        gps_loss_timeout_s=safety_cfg.get("gps_loss_watchdog_timeout_s", 5.0),
        max_mission_runtime_s=safety_cfg.get("max_mission_runtime_s", 1800.0),
        max_motor_power=nav_cfg.get("max_motor_power", 30),
        default_cruise_power=nav_cfg.get("default_cruise_power", 18),
        steering_k=nav_cfg.get("steering_k", 0.6),
    )

    # Set GCS from config
    nav.gcs_lat = cfg.get("gcs_lat")
    nav.gcs_lon = cfg.get("gcs_lon")

    # Firebase (optional)
    firebase = FirebaseSync(
        enabled=firebase_cfg.get("enabled", False),
        credentials_path=firebase_cfg.get("credentials_path", "firebase-service-account.json"),
        database_url=firebase_cfg.get("database_url", ""),
        live_path=firebase_cfg.get("live_path", firebase_cfg.get("live_collection", "live_telemetry")),
        missions_path=firebase_cfg.get("missions_path", firebase_cfg.get("missions_collection", "missions")),
        live_push_hz=firebase_cfg.get("live_push_hz", 1.0),
    )
    firebase.initialize()

    # Supabase (optional cloud relay — dashboard + command dispatch).
    # URL/key can be overridden via env vars (SUPABASE_URL / SUPABASE_KEY).
    supa_cfg = cfg.get("supabase", {})
    supabase_sync = SupabaseSync(
        url=os.environ.get("SUPABASE_URL", supa_cfg.get("url", "")),
        key=os.environ.get("SUPABASE_KEY", supa_cfg.get("key", "")),
        enabled=supa_cfg.get("enabled", False),
        telemetry_push_hz=supa_cfg.get("telemetry_push_hz", 1.0),
        command_poll_hz=supa_cfg.get("command_poll_hz", 2.0),
        sensor_batch_interval_s=supa_cfg.get("sensor_batch_interval_s", 120.0),
        system_push_hz=supa_cfg.get("system_push_hz", 1.0),
    )

    # FastAPI app
    dashboard_dir = cfg.get(
        "dashboard_dir",
        os.path.join(os.path.dirname(__file__), "..", "dashboard"),
    )
    app = create_app(
        nav_ctrl=nav,
        logger=logger,
        firebase_sync=firebase,
        broadcast_hz=telem_cfg.get("websocket_broadcast_hz", 3.0),
        dashboard_dir=dashboard_dir,
    )

    return arduino, nav, logger, firebase, supabase_sync, app, telem_cfg, nav_cfg


# ---------------------------------------------------------------------------
# Independent async tasks (§7 latency architecture)
# ---------------------------------------------------------------------------

async def _nav_decision_task(nav, interval_s: float, dwell_duration_s: float, dwell_sample_interval_s: float, logger, firebase, server_state):
    """
    Navigation decision loop — runs every 4 s (configurable).
    Latency budget: < 50 ms per tick (pure math, no I/O).
    """
    while True:
        start = time.monotonic()

        try:
            nav.nav_tick()

            # Handle DWELL state: sample sensors for the configured duration
            if nav.state.value == "DWELL":
                # Create mission record on first dwell
                if nav.dwell_mission_id is None:
                    mission_id = logger.create_mission(len(nav.waypoints))
                    nav.dwell_mission_id = mission_id

                # Collect samples at the configured interval
                sample_deadline = time.monotonic() + dwell_duration_s
                while time.monotonic() < sample_deadline:
                    remaining = sample_deadline - time.monotonic()
                    sleep_time = min(dwell_sample_interval_s, max(0.01, remaining))
                    await asyncio.sleep(sleep_time)

                    if time.monotonic() >= sample_deadline:
                        break

                    sample = nav.collect_dwell_sample()
                    if sample is not None:
                        # Write to SQLite immediately
                        try:
                            logger.log_dwell_sample(
                                mission_id=nav.dwell_mission_id,
                                waypoint_index=nav.current_waypoint_index,
                                sensors={k: v for k, v in sample.items() if k != "timestamp"},
                            )
                        except Exception as exc:
                            log.warning(f"SQLite dwell sample write error: {exc}")

                        # Push to Firebase immediately (non-blocking)
                        if firebase:
                            firebase.push_live_telemetry({
                                "type": "dwell_sample",
                                "mission_id": nav.dwell_mission_id,
                                "waypoint_index": nav.current_waypoint_index,
                                "sensors": {k: v for k, v in sample.items() if k != "timestamp"},
                                "timestamp": sample.get("timestamp", time.time()),
                            })

                        # Broadcast via WS to all connected clients
                        dwell_msg = {
                            "type": "dwell_sample",
                            "mission_id": nav.dwell_mission_id,
                            "waypoint_index": nav.current_waypoint_index,
                            "sensors": {k: v for k, v in sample.items() if k != "timestamp"},
                            "timestamp": sample.get("timestamp", time.time()),
                        }
                        try:
                            await broadcast_dwell_sample(server_state, dwell_msg)
                        except Exception as exc:
                            log.warning(f"WS dwell_sample broadcast error: {exc}")

                # Dwell complete — compute averages and advance
                avg = nav.compute_dwell_average()
                wp = nav.waypoints[nav.current_waypoint_index]

                try:
                    logger.log_waypoint_result(
                        mission_id=nav.dwell_mission_id,
                        waypoint_index=nav.current_waypoint_index,
                        lat=wp.lat,
                        lon=wp.lon,
                        arrived_at=nav._dwell_start_time,
                        sample_count=len(nav.get_dwell_samples()),
                        avg_sensors=avg,
                    )
                except Exception as exc:
                    log.warning(f"SQLite waypoint result write error: {exc}")

                # Push waypoint summary to Firebase
                if firebase:
                    firebase.push_waypoint_summary(
                        mission_id=nav.dwell_mission_id,
                        waypoint_index=nav.current_waypoint_index,
                        data={
                            "lat": wp.lat,
                            "lon": wp.lon,
                            "avg_sensors": avg,
                            "sample_count": len(nav.get_dwell_samples()),
                        },
                    )

                nav.advance_from_dwell()

                # Update mission status in SQLite if complete
                if nav.state.value == "COMPLETE" and nav.dwell_mission_id is not None:
                    try:
                        logger.update_mission_status(nav.dwell_mission_id, "COMPLETE")
                    except Exception as exc:
                        log.warning(f"SQLite mission status update error: {exc}")

        except Exception as exc:
            log.exception(f"Nav decision tick error: {exc}")

        elapsed = time.monotonic() - start
        sleep_time = max(0.0, interval_s - elapsed)
        await asyncio.sleep(sleep_time)


async def _motor_heartbeat_task(nav, hz: float):
    """
    Motor command heartbeat loop — runs every 0.2 s (default 5 Hz).
    Latency budget: < 5 ms per tick (in-memory read + serial write only).
    Re-sends the currently held (left, right) power or ping.
    """
    interval = 1.0 / hz
    _tick_count = 0
    while True:
        start = time.monotonic()
        try:
            nav.motor_heartbeat()
            _tick_count += 1
        except Exception as exc:
            log.warning(f"Motor heartbeat error: {exc}")
        elapsed = time.monotonic() - start
        sleep_time = max(0.0, interval - elapsed)
        await asyncio.sleep(sleep_time)


async def _supabase_command_drain_task(supabase_sync, nav, interval_s: float = 0.5):
    """
    Drain commands queued by SupabaseSync into the NavigationController.
    Runs on the asyncio event loop — safe to call nav methods here.
    Cadence: 2 Hz (every 0.5 s), which comfortably keeps up with the 2 Hz poll thread.
    """
    while True:
        try:
            supabase_sync.drain_commands(nav)
        except Exception as exc:
            log.warning(f"Supabase command drain error: {exc}")
        await asyncio.sleep(interval_s)


async def _periodic_sensor_task(nav, logger, supabase_sync, mission_id_fn, interval_s: float = 20.0, note_status_fn=None):
    """
    Log the current sensor reading locally every `interval_s` (default 20 s)
    and enqueue it for the cloud batch uploader (which flushes every 2 min).
    """
    while True:
        try:
            if note_status_fn is not None:
                note_status_fn()
            state = nav._latest_state
            sensors = dict(state.sensors) if getattr(state, "sensors", None) else {}
            # If no sensor data is present, push explicit 0/null values so the
            # cloud always receives a row instead of nothing.
            if not sensors:
                sensors = {"ph": 0.0, "tds": 0.0, "turb": 0.0, "wtemp": 0.0, "atemp": 0.0, "hum": 0.0}
            timestamp = time.time()
            logger.log_sensor(
                sensors=sensors,
                mission_id=mission_id_fn(),
                lat=getattr(state, "gps_lat", None),
                lon=getattr(state, "gps_lon", None),
                state=nav.state.value if nav.state else None,
                timestamp=timestamp,
            )
            if supabase_sync.enabled:
                supabase_sync.enqueue_sensor_reading({
                    "mission_id": mission_id_fn(),
                    "lat": getattr(state, "gps_lat", None),
                    "lon": getattr(state, "gps_lon", None),
                    "sensors": sensors,
                })
        except Exception as exc:
            log.warning(f"Periodic sensor log error: {exc}")
        await asyncio.sleep(interval_s)


async def _system_push_task(system_monitor, nav, supabase_sync, mission_id_fn, interval_s: float = 1.0):
    """Push Raspberry Pi processing info (pi_system) to the cloud at ~1 Hz."""
    while True:
        try:
            if supabase_sync.enabled:
                row = system_monitor.build_system_row(nav, mission_id_fn())
                supabase_sync.enqueue_system_info(row)
        except Exception as exc:
            log.warning(f"System push error: {exc}")
        await asyncio.sleep(interval_s)


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace, cfg: dict) -> None:
    arduino, nav, logger, firebase, supabase_sync, app, telem_cfg, nav_cfg = build_components(args, cfg)

    # Config values
    nav_tick_interval_s = nav_cfg.get("nav_tick_interval_s", 4.0)
    motor_heartbeat_hz = nav_cfg.get("motor_heartbeat_hz", 5.0)
    dwell_cfg = cfg.get("dwell", {})
    dwell_duration_s = dwell_cfg.get("duration_s", 30)
    dwell_sample_interval_s = dwell_cfg.get("sample_interval_s", 2)
    broadcast_hz = telem_cfg.get("websocket_broadcast_hz", 3.0)

    sensor_periodic_log_s = telem_cfg.get("periodic_log_interval_s", 20.0)
    supa_cfg = cfg.get("supabase") or {}
    system_push_hz = supa_cfg.get("system_push_hz", 1.0) if supabase_sync.enabled else 0.0
    supabase_sync_started = False

    from system_monitor import SystemMonitor
    system_monitor = SystemMonitor()

    # Resolve the active cloud mission id (set via start_mission, else local)
    def _mission_id_fn():
        return nav.mission_id

    def _note_mission_status():
        mid = nav.mission_id
        if mid is None:
            return
        st = nav.state.value if nav.state else None
        if st == "COMPLETE":
            supabase_sync.update_mission_status(mid, "COMPLETE")
        elif st == "STOPPED":
            supabase_sync.update_mission_status(mid, "STOPPED")

    # Start Supabase background threads
    if supabase_sync.enabled:
        supabase_sync.start()
        supabase_sync_started = True

    # Preload waypoints
    if args.waypoints:
        wp_path = Path(args.waypoints)
        if wp_path.exists():
            waypoints = json.loads(wp_path.read_text())
            nav.load_mission(waypoints)
            log.info(f"Loaded {len(waypoints)} waypoints from {args.waypoints}")
        else:
            log.error(f"Waypoints file not found: {args.waypoints}")

    if args.autostart:
        nav.cmd_start()
        log.info("Mission autostarted")

    # Build server state for broadcast loop
    from server import broadcast_loop, broadcast_dwell_sample, _state as server_state
    server_state["nav"] = nav
    server_state["logger"] = logger
    server_state["firebase"] = firebase
    server_state["supabase_sync"] = supabase_sync
    server_state["broadcast_interval"] = 1.0 / broadcast_hz

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = server_state["stop_event"]

    def _shutdown():
        log.info("Shutdown signal received — stopping motors and logger")
        try:
            nav.cmd_emergency_stop()
        except Exception:
            pass
        try:
            arduino.close()
        except Exception:
            pass
        try:
            logger.close()
        except Exception:
            pass
        if supabase_sync_started:
            try:
                supabase_sync.stop()
            except Exception:
                pass
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    # Launch all independent tasks
    log.info("SCRUB v4 nav_service starting...")

    tasks = [
        asyncio.create_task(
            _nav_decision_task(nav, nav_tick_interval_s, dwell_duration_s, dwell_sample_interval_s, logger, firebase, server_state)
        ),
        asyncio.create_task(_motor_heartbeat_task(nav, motor_heartbeat_hz)),
        asyncio.create_task(broadcast_loop(server_state)),
    ]

    if firebase._enabled and firebase._initialized:
        tasks.append(asyncio.create_task(firebase.run_consumer()))

    # Periodic local sensor logging (every 20 s) — always runs; cloud enqueue
    # is gated inside the task on supabase_sync.enabled.
    tasks.append(asyncio.create_task(
        _periodic_sensor_task(nav, logger, supabase_sync, _mission_id_fn, sensor_periodic_log_s, note_status_fn=_note_mission_status)
    ))

    # Supabase command drain + Pi system monitor push
    if supabase_sync.enabled:
        tasks.append(asyncio.create_task(_supabase_command_drain_task(supabase_sync, nav)))
        tasks.append(asyncio.create_task(
            _system_push_task(system_monitor, nav, supabase_sync, _mission_id_fn, 1.0 / max(system_push_hz, 0.1))
        ))

    try:
        import uvicorn
        config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
        server = uvicorn.Server(config)
        tasks.append(asyncio.create_task(server.serve()))

        await stop_event.wait()
    except Exception as exc:
        log.exception(f"Unhandled exception: {exc}")
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        arduino.close()
        logger.close()
        if supabase_sync_started:
            supabase_sync.stop()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    cfg = load_config(args.config)

    try:
        asyncio.run(run(args, cfg))
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    sys.exit(0)


if __name__ == "__main__":
    main()
