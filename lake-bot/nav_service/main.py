"""
main.py — CLI entrypoint for Lake Bot nav_service.

Usage:
  # Dashboard-driven (waits for commands over WebSocket):
  python3 main.py --config config.yaml --motors mock --simulate-gps

  # CLI-only autostart (bench test, no dashboard needed):
  python3 main.py --config config.yaml --motors mock --simulate-gps \
      --waypoints waypoints_sample.json --autostart

All CLI flags override config.yaml for that run only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
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
        description="Lake Bot Navigation Service",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml",
    )
    p.add_argument(
        "--motors",
        choices=["mock", "gpio"],
        default="mock",
        help="Motor driver backend",
    )
    p.add_argument(
        "--simulate-gps",
        action="store_true",
        help="Use SimulatedGpsDriver instead of real UART GPS",
    )
    p.add_argument(
        "--simulate-gps-track",
        default=None,
        metavar="PATH",
        help="JSON track file for SimulatedGpsDriver (optional)",
    )
    p.add_argument(
        "--waypoints",
        default=None,
        metavar="PATH",
        help="JSON waypoints file to preload (list of {lat, lon})",
    )
    p.add_argument(
        "--autostart",
        action="store_true",
        help="Immediately start the mission after loading waypoints",
    )
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
    from logger import NavLogger
    from gps_driver import GpsDriver, SimulatedGpsDriver
    from compass_driver import CompassDriver
    from motor_driver import GpioMotorDriver, MockMotorDriver
    from navigation import NavigationController
    from telemetry_server import TelemetryServer

    # Extract config sections (with defaults)
    nav_cfg = cfg.get("navigation", {})
    safety_cfg = cfg.get("safety", {})
    gps_cfg = cfg.get("gps", {})
    compass_cfg = cfg.get("compass", {})
    motors_cfg = cfg.get("motors", {})
    telem_cfg = cfg.get("telemetry", {})
    logger_cfg = cfg.get("logger", {})

    # Logger
    db_path = os.path.join(os.path.dirname(__file__), logger_cfg.get("db_path", "nav_log.db"))
    logger = NavLogger(db_path=db_path)

    # GPS driver
    if args.simulate_gps:
        gps = SimulatedGpsDriver(track_file=args.simulate_gps_track)
    else:
        gps = GpsDriver(
            device=gps_cfg.get("device", "/dev/ttyAMA0"),
            baud=gps_cfg.get("baud", 9600),
        )

    gps.open()

    # Compass driver
    compass = CompassDriver(
        bus=compass_cfg.get("i2c_bus", 1),
        address=compass_cfg.get("i2c_address", 0x0D),
    )
    if not args.simulate_gps:  # Only try I2C on real hardware
        try:
            compass.open()
        except Exception as exc:
            log.warning(f"Compass open failed (will fall back to GPS course): {exc}")

    # Motor driver
    if args.motors == "gpio":
        motors = GpioMotorDriver(
            left_pwm_pin=motors_cfg.get("left_pwm_pin", 12),
            left_dir_a_pin=motors_cfg.get("left_dir_a_pin", 5),
            left_dir_b_pin=motors_cfg.get("left_dir_b_pin", 6),
            right_pwm_pin=motors_cfg.get("right_pwm_pin", 13),
            right_dir_a_pin=motors_cfg.get("right_dir_a_pin", 16),
            right_dir_b_pin=motors_cfg.get("right_dir_b_pin", 20),
            pwm_frequency=motors_cfg.get("pwm_frequency", 1000),
            max_power=nav_cfg.get("max_motor_power", 30),
        )
    else:
        motors = MockMotorDriver(logger=logger, max_power=nav_cfg.get("max_motor_power", 30))

    # Navigation controller
    nav = NavigationController(
        gps=gps,
        compass=compass,
        motors=motors,
        logger=logger,
        arrival_radius_m=nav_cfg.get("waypoint_arrival_radius_m", 2.5),
        geofence_radius_m=safety_cfg.get("geofence_radius_m", 150.0),
        gps_loss_timeout_s=safety_cfg.get("gps_loss_watchdog_timeout_s", 5.0),
        max_mission_runtime_s=safety_cfg.get("max_mission_runtime_s", 1800.0),
        max_motor_power=nav_cfg.get("max_motor_power", 30),
        default_cruise_power=nav_cfg.get("default_cruise_power", 18),
        steering_k=nav_cfg.get("steering_k", 0.6),
    )

    # Telemetry server
    server = TelemetryServer(
        nav_ctrl=nav,
        logger=logger,
        host="0.0.0.0",
        port=telem_cfg.get("websocket_port", 8765),
        broadcast_rate_hz=telem_cfg.get("broadcast_rate_hz", 3.0),
    )

    return gps, compass, motors, nav, logger, server


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace, cfg: dict) -> None:
    gps, compass, motors, nav, logger, server = build_components(args, cfg)

    # Preload waypoints if provided
    if args.waypoints:
        wp_path = Path(args.waypoints)
        if wp_path.exists():
            waypoints = json.loads(wp_path.read_text())
            nav.load_mission(waypoints)
            log.info(f"Loaded {len(waypoints)} waypoints from {args.waypoints}")
        else:
            log.error(f"Waypoints file not found: {args.waypoints}")

    # Autostart
    if args.autostart:
        nav.cmd_start()
        log.info("Mission autostarted")

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_running_loop()

    def _shutdown():
        log.info("Shutdown signal received — stopping motors and server")
        try:
            motors.stop()
        except Exception:
            pass
        try:
            logger.close()
        except Exception:
            pass
        server.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for all signals
            pass

    log.info("Lake Bot nav_service starting...")
    try:
        await server.serve()
    except Exception as exc:
        log.exception(f"Unhandled exception in serve(): {exc}")
    finally:
        motors.stop()
        logger.close()
        gps.close()


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
