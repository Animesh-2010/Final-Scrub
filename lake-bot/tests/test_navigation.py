"""
tests/test_navigation.py — Unit tests for navigation.py math and state machine.

No hardware; MockMotorDriver used throughout.
"""

import math
from unittest.mock import MagicMock, call, create_autospec

import pytest

from navigation import (
    NavState,
    NavigationController,
    bearing,
    compute_wheel_powers,
    haversine_distance,
    normalize_heading_error,
)
from motor_driver import MockMotorDriver
from gps_driver import FixResult, SimulatedGpsDriver
from compass_driver import CompassDriver
from logger import NavLogger


# ─── Helpers ──────────────────────────────────────────────────────────

def _make_nav(
    waypoints=None,
    gps_fix=None,
    compass_heading=None,
    compass_raises=False,
) -> tuple[NavigationController, MockMotorDriver, NavLogger]:
    """Build a NavigationController with mocked dependencies."""
    gps = MagicMock(spec=SimulatedGpsDriver)
    if gps_fix is not None:
        gps.get_fix.return_value = gps_fix
    else:
        gps.get_fix.return_value = FixResult(
            lat=12.91686, lon=77.48698, fix_quality=1, satellites=8,
        )

    compass = MagicMock(spec=CompassDriver)
    if compass_raises:
        compass.get_heading.side_effect = OSError("no compass")
    elif compass_heading is not None:
        compass.get_heading.return_value = compass_heading
    else:
        compass.get_heading.return_value = 0.0

    motors = MockMotorDriver()
    logger = MagicMock(spec=NavLogger)

    nav = NavigationController(
        gps=gps,
        compass=compass,
        motors=motors,
        logger=logger,
        arrival_radius_m=2.5,
        geofence_radius_m=150.0,
        gps_loss_timeout_s=5.0,
        max_mission_runtime_s=1800.0,
        max_motor_power=30,
        default_cruise_power=18,
        steering_k=0.6,
    )

    if waypoints:
        nav.load_mission(waypoints)

    return nav, motors, logger


# ─── Haversine ────────────────────────────────────────────────────────

def test_haversine_known_distance():
    """
    Distance between two points ~111 m apart (1 arc-second of latitude ≈ 30.9 m,
    so ~0.001° ≈ 111 m). Pre-computed expected: ≈111.2 m.
    """
    lat1, lon1 = 12.91686, 77.48698
    lat2, lon2 = 12.91786, 77.48698   # 0.001° north
    dist = haversine_distance(lat1, lon1, lat2, lon2)
    # 0.001° lat ≈ 111.2 m at any longitude
    assert abs(dist - 111.2) < 1.0, f"haversine {dist} not ≈ 111.2 m"


# ─── Bearing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("lat1,lon1,lat2,lon2,expected_deg,tol", [
    # Due north: moving positive latitude from same longitude
    (0.0, 0.0, 1.0, 0.0, 0.0, 1.0),
    # Due east: moving positive longitude from same latitude
    (0.0, 0.0, 0.0, 1.0, 90.0, 1.0),
    # Due south: moving negative latitude
    (1.0, 0.0, 0.0, 0.0, 180.0, 1.0),
    # Due west: moving negative longitude
    (0.0, 1.0, 0.0, 0.0, 270.0, 1.0),
])
def test_bearing_known_values(lat1, lon1, lat2, lon2, expected_deg, tol):
    result = bearing(lat1, lon1, lat2, lon2)
    assert abs(result - expected_deg) < tol, f"bearing {result} != {expected_deg}°"


# ─── Normalize heading error ──────────────────────────────────────────

@pytest.mark.parametrize("target,current,expected", [
    (350, 10,  -20),    # wrap: 350–10 = 340 → normalise to -20
    (10,  350,  20),    # wrap: 10–350 = -340 → normalise to 20
    (90,  45,   45),    # positive, no wrap
    (45,  90,  -45),    # negative, no wrap
    (0,   0,    0),     # zero
    (180, 0,   180),    # ±180 boundary — +180 is in (-180, 180]
])
def test_normalize_heading_error_wraparound(target, current, expected):
    result = normalize_heading_error(target, current)
    assert abs(result - expected) < 1e-9, f"normalize({target},{current}) = {result}, expected {expected}"


def test_normalize_heading_error_180_boundary_is_either_sign():
    """normalize_heading_error(180, 0) should yield either +180 or -180 (both are equivalent)."""
    result = normalize_heading_error(180, 0)
    assert abs(abs(result) - 180) < 1e-9, f"normalize(180, 0) = {result}, expected ±180"


# ─── Wheel powers ─────────────────────────────────────────────────────

def test_compute_wheel_powers_straight_ahead():
    """heading_error=0 → left == right == base_power."""
    left, right = compute_wheel_powers(0, base_power=18, k=0.6, max_power=30)
    assert left == 18
    assert right == 18


def test_compute_wheel_powers_turn_right():
    """Positive heading_error (need to turn right) → left > right."""
    left, right = compute_wheel_powers(10, base_power=18, k=0.6, max_power=30)
    assert left > right, f"expected left > right, got left={left} right={right}"


def test_compute_wheel_powers_turn_left():
    """Negative heading_error (need to turn left) → right > left."""
    left, right = compute_wheel_powers(-10, base_power=18, k=0.6, max_power=30)
    assert right > left


def test_compute_wheel_powers_clamps_to_max():
    """Extreme heading_error → both outputs stay within ±max_power."""
    max_p = 30
    left, right = compute_wheel_powers(1000, base_power=18, k=0.6, max_power=max_p)
    assert -max_p <= left <= max_p
    assert -max_p <= right <= max_p

    left2, right2 = compute_wheel_powers(-1000, base_power=18, k=0.6, max_power=max_p)
    assert -max_p <= left2 <= max_p
    assert -max_p <= right2 <= max_p


# ─── Waypoint arrival ────────────────────────────────────────────────

def test_waypoint_arrival_within_radius():
    """2.0 m < 2.5 m radius → arrived."""
    # Place current position 2.0 m north of waypoint
    dist = haversine_distance(12.91686, 77.48698, 12.91704, 77.48698)
    # ~2.0 m from ~12.917
    # Use actual distance assertion instead of hardcoded
    lat_offset = 2.0 / 111320.0   # ~2 m north
    dist = haversine_distance(12.91686, 77.48698,
                               12.91686 + lat_offset, 77.48698)
    assert dist < 2.5, f"distance {dist} should be < 2.5 m"


def test_waypoint_arrival_outside_radius():
    """3.0 m > 2.5 m radius → not arrived."""
    lat_offset = 3.0 / 111320.0   # ~3 m north
    dist = haversine_distance(12.91686, 77.48698,
                               12.91686 + lat_offset, 77.48698)
    assert dist > 2.5, f"distance {dist} should be > 2.5 m"


# ─── State machine — happy path ───────────────────────────────────────

def test_state_transitions_full_happy_path():
    """IDLE→start→RUNNING→pause→PAUSED→start→RUNNING→(all wps reached)→COMPLETE."""
    # Place two waypoints very close to the default GPS fix position so they
    # are immediately "reached" on the first tick
    base_lat = 12.91686
    base_lon = 77.48698

    wp1 = {"lat": base_lat + 0.000001, "lon": base_lon}   # ~0.11 m away
    wp2 = {"lat": base_lat + 0.000002, "lon": base_lon}   # ~0.22 m away

    nav, motors, logger = _make_nav(waypoints=[wp1, wp2])

    assert nav.state == NavState.IDLE

    nav.cmd_start()
    assert nav.state == NavState.RUNNING

    nav.cmd_pause()
    assert nav.state == NavState.PAUSED

    nav.cmd_start()
    assert nav.state == NavState.RUNNING

    # Tick until COMPLETE (max 20 ticks)
    for _ in range(20):
        nav.tick()
        if nav.state != NavState.RUNNING:
            break

    assert nav.state == NavState.COMPLETE


def test_state_transition_stop_from_running():
    """stop from RUNNING → STOPPED; motors.stop() called."""
    nav, motors, logger = _make_nav(waypoints=[{"lat": 99.0, "lon": 99.0}])
    nav.cmd_start()
    assert nav.state == NavState.RUNNING

    nav.cmd_stop()
    assert nav.state == NavState.STOPPED
    assert motors.stop_call_count >= 1


def test_state_transition_stop_from_paused():
    """stop from PAUSED → STOPPED; motors.stop() called."""
    nav, motors, logger = _make_nav(waypoints=[{"lat": 99.0, "lon": 99.0}])
    nav.cmd_start()
    nav.cmd_pause()
    assert nav.state == NavState.PAUSED

    stop_before = motors.stop_call_count
    nav.cmd_stop()
    assert nav.state == NavState.STOPPED
    assert motors.stop_call_count > stop_before


def test_start_rejected_with_empty_waypoint_list():
    """start with no waypoints loaded → remains IDLE, error logged."""
    nav, motors, logger = _make_nav()   # no waypoints
    nav.cmd_start()
    assert nav.state == NavState.IDLE
    logger.log_event.assert_called()
    # Verify the error event was logged
    calls_str = str(logger.log_event.call_args_list)
    assert "error" in calls_str.lower() or "rejected" in calls_str.lower()
