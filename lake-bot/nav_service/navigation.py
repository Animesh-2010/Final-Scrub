"""
navigation.py — Bearing/distance math, steering, and mission state machine
for Lake Bot nav_service.

State machine states: IDLE, RUNNING, PAUSED, COMPLETE, STOPPED, GEOFENCE_STOP, GPS_LOST
"""

from __future__ import annotations

import logging
import math
import time
from enum import Enum, auto
from typing import Optional

from gps_driver import FixResult, GpsDriver
from compass_driver import CompassDriver
from motor_driver import MotorDriver
from safety import GpsWatchdog, MissionTimer, check_geofence
from logger import NavLogger


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class NavState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    STOPPED = "STOPPED"
    GEOFENCE_STOP = "GEOFENCE_STOP"
    GPS_LOST = "GPS_LOST"


# ---------------------------------------------------------------------------
# Pure math helpers (no external geo library)
# ---------------------------------------------------------------------------

_EARTH_RADIUS_M = 6_371_000.0


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in metres between two WGS-84 lat/lon points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Return initial bearing from point 1 to point 2 in degrees [0, 360).
    0 = north, 90 = east, 180 = south, 270 = west.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    x = math.sin(dlambda) * math.cos(phi2)
    y = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    )
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def normalize_heading_error(target_bearing: float, current_heading: float) -> float:
    """
    Return the signed angular difference target_bearing − current_heading,
    normalised to the range (-180, 180].
    Positive = need to turn right, negative = need to turn left.
    """
    error = (target_bearing - current_heading) % 360
    if error > 180:
        error -= 360
    return error


def compute_wheel_powers(
    heading_error_deg: float,
    base_power: int,
    k: float = 0.6,
    max_power: int = 30,
) -> tuple[int, int]:
    """
    Proportional steering mixer.

    left  = clamp(base_power + k * heading_error_deg, -max_power, max_power)
    right = clamp(base_power - k * heading_error_deg, -max_power, max_power)

    Returns (left_power, right_power) as integers.
    """
    left = base_power + k * heading_error_deg
    right = base_power - k * heading_error_deg
    left_int = int(round(max(-max_power, min(max_power, left))))
    right_int = int(round(max(-max_power, min(max_power, right))))
    return left_int, right_int


# ---------------------------------------------------------------------------
# Waypoint
# ---------------------------------------------------------------------------

class Waypoint:
    def __init__(self, lat: float, lon: float):
        self.lat = lat
        self.lon = lon


# ---------------------------------------------------------------------------
# Navigation controller (state machine)
# ---------------------------------------------------------------------------

class NavigationController:
    """
    Orchestrates the mission state machine and per-tick navigation decisions.

    Call tick() at the telemetry rate (3 Hz) to advance the state machine.
    """

    def __init__(
        self,
        gps: GpsDriver,
        compass: CompassDriver,
        motors: MotorDriver,
        logger: NavLogger,
        *,
        arrival_radius_m: float = 2.5,
        geofence_radius_m: float = 150.0,
        gps_loss_timeout_s: float = 5.0,
        max_mission_runtime_s: float = 1800.0,
        max_motor_power: int = 30,
        default_cruise_power: int = 18,
        steering_k: float = 0.6,
    ):
        self._gps = gps
        self._compass = compass
        self._motors = motors
        self._logger = logger

        self._arrival_radius_m = arrival_radius_m
        self._geofence_radius_m = geofence_radius_m
        self._max_motor_power = max_motor_power
        self._cruise_power = default_cruise_power
        self._steering_k = steering_k

        self.state: NavState = NavState.IDLE
        self.waypoints: list[Waypoint] = []
        self.current_waypoint_index: int = 0

        # GCS position (set by dashboard command)
        self.gcs_lat: Optional[float] = None
        self.gcs_lon: Optional[float] = None

        # Last known fix
        self.last_fix: Optional[FixResult] = None

        # Safety helpers
        self._watchdog = GpsWatchdog(timeout_s=gps_loss_timeout_s)
        self._mission_timer = MissionTimer(max_runtime_s=max_mission_runtime_s)

        # Left/right power at last tick (for telemetry)
        self.left_power: int = 0
        self.right_power: int = 0

        # Last heading used for steering (reported in telemetry)
        self.heading: float = 0.0

        # Compass fallback warning flag
        self._compass_fallback_warned: bool = False

    # ------------------------------------------------------------------
    # Speed control
    # ------------------------------------------------------------------

    def set_cruise_power(self, power: int) -> None:
        """Update cruise power (capped to max_motor_power)."""
        self._cruise_power = max(0, min(self._max_motor_power, power))

    # ------------------------------------------------------------------
    # State-machine transitions
    # ------------------------------------------------------------------

    def _transition(self, new_state: NavState, detail: str = "") -> None:
        old = self.state
        self.state = new_state
        msg = f"{old.value} -> {new_state.value}" + (f": {detail}" if detail else "")
        log.info(msg)
        self._logger.log_event("state_transition", msg)

    def load_mission(self, waypoints: list[dict]) -> None:
        """Load a new waypoint list (dict with 'lat'/'lon'). Resets index."""
        self.waypoints = [Waypoint(wp["lat"], wp["lon"]) for wp in waypoints]
        self.current_waypoint_index = 0
        self._logger.log_event("load_mission", f"{len(self.waypoints)} waypoints loaded")

    def cmd_start(self) -> None:
        if self.state == NavState.IDLE:
            if not self.waypoints:
                log.error("start rejected: no waypoints loaded")
                self._logger.log_event("error", "start rejected: no waypoints loaded")
                return
            self.current_waypoint_index = 0
            self._mission_timer.start()
            self._watchdog.feed()
            self._transition(NavState.RUNNING)
        elif self.state == NavState.PAUSED:
            self._transition(NavState.RUNNING)
        else:
            log.warning(f"start ignored in state {self.state.value}")

    def cmd_pause(self) -> None:
        if self.state == NavState.RUNNING:
            self._motors.stop()
            self.left_power = 0
            self.right_power = 0
            self._transition(NavState.PAUSED)
        else:
            log.warning(f"pause ignored in state {self.state.value}")

    def cmd_stop(self) -> None:
        if self.state in (NavState.RUNNING, NavState.PAUSED):
            self._motors.stop()
            self.left_power = 0
            self.right_power = 0
            self._transition(NavState.STOPPED)
        else:
            log.warning(f"stop ignored in state {self.state.value}")

    def cmd_emergency_stop(self) -> None:
        """Immediately stop motors from any state."""
        self._motors.stop()
        self.left_power = 0
        self.right_power = 0
        self._transition(NavState.STOPPED, "emergency stop")

    # ------------------------------------------------------------------
    # Per-tick logic
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """
        Execute one navigation tick. Called at 3 Hz by the telemetry loop.
        Updates self.last_fix, self.left_power, self.right_power, self.state.
        """
        # --- Get GPS fix ---
        try:
            fix = self._gps.get_fix()
            if fix.fix_quality > 0:
                self._watchdog.feed()
            self.last_fix = fix
        except Exception as exc:
            log.warning(f"GPS read error: {exc}")
            fix = self.last_fix  # use stale data

        # --- Safety checks (only when RUNNING) ---
        if self.state == NavState.RUNNING:
            # GPS loss watchdog
            if self._watchdog.expired():
                self._motors.stop()
                self.left_power = 0
                self.right_power = 0
                self._transition(NavState.GPS_LOST, "GPS loss watchdog expired")
                return

            # Mission timer
            if self._mission_timer.expired():
                self._motors.stop()
                self.left_power = 0
                self.right_power = 0
                self._transition(NavState.STOPPED, "max mission runtime exceeded")
                return

            # Geofence (only if GCS is set and we have a valid fix)
            if (
                self.gcs_lat is not None
                and fix is not None
                and fix.fix_quality > 0
            ):
                if not check_geofence(
                    fix.lat, fix.lon,
                    self.gcs_lat, self.gcs_lon,
                    self._geofence_radius_m,
                ):
                    self._motors.stop()
                    self.left_power = 0
                    self.right_power = 0
                    self._transition(NavState.GEOFENCE_STOP, "geofence breach detected")
                    return

        # --- Navigation steering (only when RUNNING with a valid fix) ---
        if self.state == NavState.RUNNING and fix is not None and self.waypoints:
            wp = self.waypoints[self.current_waypoint_index]

            dist = haversine_distance(fix.lat, fix.lon, wp.lat, wp.lon)

            if dist <= self._arrival_radius_m:
                # Reached this waypoint
                self.current_waypoint_index += 1
                if self.current_waypoint_index >= len(self.waypoints):
                    self._motors.stop()
                    self.left_power = 0
                    self.right_power = 0
                    self._transition(NavState.COMPLETE, "all waypoints reached")
                    return
                else:
                    log.info(f"Waypoint {self.current_waypoint_index - 1} reached, advancing")
                    self._logger.log_event(
                        "waypoint_reached",
                        f"index={self.current_waypoint_index - 1}",
                    )
                    wp = self.waypoints[self.current_waypoint_index]
                    dist = haversine_distance(fix.lat, fix.lon, wp.lat, wp.lon)

            # Get heading
            try:
                heading = self._compass.get_heading()
            except Exception:
                if not self._compass_fallback_warned:
                    log.warning("Compass unavailable, falling back to GPS course_deg")
                    self._compass_fallback_warned = True
                heading = fix.course_deg

            self.heading = heading

            target_brg = bearing(fix.lat, fix.lon, wp.lat, wp.lon)
            error = normalize_heading_error(target_brg, heading)

            left, right = compute_wheel_powers(
                error,
                self._cruise_power,
                k=self._steering_k,
                max_power=self._max_motor_power,
            )

            self._motors.set_left(left)
            self._motors.set_right(right)
            self.left_power = left
            self.right_power = right

        elif self.state not in (NavState.RUNNING,):
            # Ensure motors are off when not running
            self.left_power = 0
            self.right_power = 0
