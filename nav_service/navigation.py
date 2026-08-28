"""
navigation.py — Bearing/distance math, steering, and mission state machine
for SCRUB v4 nav_service.

State machine states: IDLE, RUNNING, PAUSED, COMPLETE, STOPPED,
                      GEOFENCE_STOP, GPS_LOST, MANUAL, DWELL
"""

from __future__ import annotations

import logging
import math
import time
from enum import Enum
from typing import Optional

from arduino_link import ArduinoState
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
    MANUAL = "MANUAL"
    DWELL = "DWELL"


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
    Return the signed angular difference target_bearing - current_heading,
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

    Decoupled architecture:
    - nav_tick(): called every 4 s, computes new heading/power targets
    - motor_heartbeat(): called every 0.2 s, re-sends current power values
    - Both read from self._latest_state (in-memory), never blocking on I/O.
    """

    def __init__(
        self,
        arduino,
        logger: NavLogger,
        *,
        arrival_radius_m: float = 2.0,
        geofence_radius_m: float = 150.0,
        gps_loss_timeout_s: float = 5.0,
        max_mission_runtime_s: float = 1800.0,
        max_motor_power: int = 30,
        default_cruise_power: int = 18,
        steering_k: float = 0.6,
        dashboard_override_enabled: bool = False,
    ):
        self._arduino = arduino
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

        # Last known Arduino state
        self._latest_state: ArduinoState = ArduinoState()

        # Currently held motor power (recomputed every 4 s, sent every 0.2 s)
        self.left_power: int = 0
        self.right_power: int = 0

        # Last heading used for steering (reported in telemetry)
        self.heading: float = 0.0

        # Navigation telemetry (reported in telemetry broadcast)
        self.target_bearing: float = 0.0
        self.heading_error: float = 0.0
        self.distance_to_target: float = 0.0

        # Safety helpers
        self._watchdog = GpsWatchdog(timeout_s=gps_loss_timeout_s)
        self._mission_timer = MissionTimer(max_runtime_s=max_mission_runtime_s)

        # Dashboard manual-override toggle
        self._dashboard_override = dashboard_override_enabled

        # Dwell state
        self._dwell_samples: list[dict[str, float]] = []
        self._dwell_start_time: float = 0.0
        self._dwell_mission_id: int | None = None

        # Mission ID for logging
        self._current_mission_id: int | None = None

        # Cloud mission id (set when started via Supabase start_mission)
        self.mission_id: int | None = None

        # For Pi system info reporting
        self.last_nav_tick_ms: float = 0.0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Effective mode
    # ------------------------------------------------------------------

    def _effective_mode(self) -> str:
        """
        Compute effective mode per spec:
        MANUAL if (physical_rc_mode == "MANUAL" or dashboard_override_enabled)
        else AUTO
        """
        physical_mode = self._latest_state.mode
        if physical_mode == "MANUAL" or self._dashboard_override:
            return "MANUAL"
        return "AUTO"

    # ------------------------------------------------------------------
    # Speed control
    # ------------------------------------------------------------------

    def set_cruise_power(self, power: int) -> None:
        """Update cruise power (capped to max_motor_power)."""
        self._cruise_power = max(0, min(self._max_motor_power, power))

    # ------------------------------------------------------------------
    # Dashboard override
    # ------------------------------------------------------------------

    def set_dashboard_override(self, enabled: bool) -> None:
        self._dashboard_override = enabled
        log.info(f"Dashboard override: {'ENABLED' if enabled else 'DISABLED'}")
        self._logger.log_event("dashboard_override", f"enabled={enabled}")

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
        elif self.state in (NavState.PAUSED, NavState.MANUAL):
            self._transition(NavState.RUNNING)
        else:
            log.warning(f"start ignored in state {self.state.value}")

    def cmd_pause(self) -> None:
        if self.state == NavState.RUNNING:
            self.left_power = 0
            self.right_power = 0
            self._transition(NavState.PAUSED)
        else:
            log.warning(f"pause ignored in state {self.state.value}")

    def cmd_stop(self) -> None:
        if self.state in (NavState.RUNNING, NavState.PAUSED, NavState.DWELL):
            self.left_power = 0
            self.right_power = 0
            self._transition(NavState.STOPPED)
        else:
            log.warning(f"stop ignored in state {self.state.value}")

    def cmd_emergency_stop(self) -> None:
        """Immediately stop motors from any state."""
        self.left_power = 0
        self.right_power = 0
        self._transition(NavState.STOPPED, "emergency stop")

    # ------------------------------------------------------------------
    # Nav decision tick (4 s cadence)
    # ------------------------------------------------------------------

    def nav_tick(self) -> None:
        """
        Execute one navigation decision tick. Called every 4 s.
        Reads latest Arduino state, computes new power targets.
        """
        _tick_start = time.monotonic()
        self.last_error = None
        # Pull latest state from Arduino link
        try:
            state = self._arduino.get_latest_state()
            self._latest_state = state
        except Exception as exc:
            log.warning(f"Arduino read error: {exc}")
            self.last_error = f"arduino: {exc}"
            state = self._latest_state

        # Safety checks (only when RUNNING) — check BEFORE feeding watchdog
        if self.state == NavState.RUNNING:
            # GPS loss watchdog
            if self._watchdog.expired():
                self.left_power = 0
                self.right_power = 0
                self._transition(NavState.GPS_LOST, "GPS loss watchdog expired")
                return

            # Mission timer
            if self._mission_timer.expired():
                self.left_power = 0
                self.right_power = 0
                self._transition(NavState.STOPPED, "max mission runtime exceeded")
                return

            # Geofence (only if GCS is set and we have a valid fix)
            if (
                self.gcs_lat is not None
                and state.gps_fix > 0
            ):
                if not check_geofence(
                    state.gps_lat, state.gps_lon,
                    self.gcs_lat, self.gcs_lon,
                    self._geofence_radius_m,
                ):
                    self.left_power = 0
                    self.right_power = 0
                    self._transition(NavState.GEOFENCE_STOP, "geofence breach detected")
                    return

        # Feed watchdog AFTER safety checks (so expired() can detect loss first)
        if state.gps_fix > 0:
            self._watchdog.feed()

        # Compute effective mode
        effective = self._effective_mode()

        # Handle mode transitions
        if effective == "MANUAL":
            if self.state in (NavState.RUNNING, NavState.DWELL):
                if self.state == NavState.DWELL:
                    log.warning("Manual override during dwell — discarding partial samples")
                    self._dwell_samples.clear()
                self._transition(NavState.MANUAL, "effective mode is MANUAL")
            return

        # If in MANUAL state and mode returns to AUTO, go to IDLE (require explicit Start)
        if self.state == NavState.MANUAL:
            self._transition(NavState.IDLE, "effective mode returned to AUTO — press Start to resume")
            return

        # If in DWELL: check if dwell timer expired and auto-advance
        if self.state == NavState.DWELL:
            if self.dwell_is_expired():
                avg = self.compute_dwell_average()
                wp = self.waypoints[self.current_waypoint_index]
                # Dwell result will be logged by the caller (main.py dwell loop)
                self.advance_from_dwell()
            return

        # Navigation steering (only when RUNNING with valid fix and waypoints)
        if self.state == NavState.RUNNING and state.gps_fix > 0 and self.waypoints:
            wp = self.waypoints[self.current_waypoint_index]

            dist = haversine_distance(state.gps_lat, state.gps_lon, wp.lat, wp.lon)

            if dist <= self._arrival_radius_m:
                # Reached this waypoint — transition to DWELL
                self.left_power = 0
                self.right_power = 0
                self._dwell_samples.clear()
                self._dwell_start_time = time.monotonic()
                self._transition(NavState.DWELL, f"waypoint {self.current_waypoint_index} reached")
                return

            # Use Arduino heading (compass wired to Arduino)
            self.heading = state.heading

            target_brg = bearing(state.gps_lat, state.gps_lon, wp.lat, wp.lon)
            error = normalize_heading_error(target_brg, state.heading)

            self.target_bearing = target_brg
            self.heading_error = error
            self.distance_to_target = dist

            left, right = compute_wheel_powers(
                error,
                self._cruise_power,
                k=self._steering_k,
                max_power=self._max_motor_power,
            )

            self.left_power = left
            self.right_power = right

        elif self.state not in (NavState.RUNNING, NavState.DWELL):
            self.left_power = 0
            self.right_power = 0

        self.last_nav_tick_ms = round((time.monotonic() - _tick_start) * 1000.0, 2)

    # ------------------------------------------------------------------
    # Motor heartbeat (0.2 s cadence)
    # ------------------------------------------------------------------

    def motor_heartbeat(self) -> None:
        """
        Re-send the currently held motor power to the Arduino.
        Runs independently at 5 Hz, not coupled to the 4 s nav tick.
        """
        effective = self._effective_mode()

        if self.state == NavState.RUNNING and effective == "AUTO":
            self._arduino.send_motor_command(self.left_power, self.right_power)
        else:
            self._arduino.send_ping()

    # ------------------------------------------------------------------
    # Dwell sampling (called from the dwell loop in main.py)
    # ------------------------------------------------------------------

    def collect_dwell_sample(self) -> dict[str, float] | None:
        """
        Collect one sensor sample during dwell. Returns the sample dict
        or None if Arduino state is unavailable.
        """
        state = self._latest_state
        if not state.sensors:
            return None
        sample = dict(state.sensors)
        sample["timestamp"] = time.time()
        self._dwell_samples.append(sample)
        return sample

    def get_dwell_samples(self) -> list[dict[str, float]]:
        return list(self._dwell_samples)

    def compute_dwell_average(self) -> dict[str, float]:
        """
        Compute the mean of every sensor key across dwell samples.
        Skip samples missing a key rather than crashing.
        """
        if not self._dwell_samples:
            return {}

        all_keys = set()
        for s in self._dwell_samples:
            all_keys.update(k for k in s if k != "timestamp")

        averages = {}
        for key in all_keys:
            vals = [s[key] for s in self._dwell_samples if key in s and isinstance(s[key], (int, float))]
            if vals:
                averages[key] = round(sum(vals) / len(vals), 2)
        return averages

    def advance_from_dwell(self) -> None:
        """Advance to next waypoint after dwell completes."""
        self.current_waypoint_index += 1
        self._dwell_samples.clear()

        if self.current_waypoint_index >= len(self.waypoints):
            self._transition(NavState.COMPLETE, "all waypoints reached")
        else:
            self._transition(NavState.RUNNING, f"advancing to waypoint {self.current_waypoint_index}")

    def dwell_is_expired(self, duration_s: float = 30.0) -> bool:
        """Check if dwell window has elapsed."""
        return (time.monotonic() - self._dwell_start_time) >= duration_s

    @property
    def dwell_mission_id(self) -> int | None:
        return self._dwell_mission_id

    @dwell_mission_id.setter
    def dwell_mission_id(self, val: int | None) -> None:
        self._dwell_mission_id = val
