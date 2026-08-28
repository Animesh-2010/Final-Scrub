"""
tests/test_integration_simulated_mission.py — End-to-end simulated mission tests.

Uses SimulatedGpsDriver (track file) + MockMotorDriver — no hardware.
Tests: complete mission, geofence breach, GPS loss.
"""

import json
import os
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from gps_driver import SimulatedGpsDriver, FixResult
from compass_driver import CompassDriver
from motor_driver import MockMotorDriver
from navigation import NavigationController, NavState
from logger import NavLogger


# ─── Helper: build a nav controller with a track-following GPS ────────

def _make_nav_with_track(track: list[dict], waypoints: list[dict], *, offset_s: float = 0.0):
    """
    Build a NavigationController with a SimulatedGpsDriver playing back
    the given track, and the given mission waypoints preloaded.

    offset_s: pretend the sim started offset_s seconds ago.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(track, f)
        track_path = f.name

    gps = SimulatedGpsDriver(track_file=track_path)
    gps.open()
    if offset_s:
        gps._start_time -= offset_s   # fast-forward time

    compass = MagicMock(spec=CompassDriver)
    compass.get_heading.side_effect = OSError("no compass")   # force GPS fallback

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
    nav.load_mission(waypoints)

    return nav, motors, track_path


def _run_nav_until_done(nav, max_ticks: int = 300) -> NavState:
    """Tick nav until it leaves RUNNING (or we hit max_ticks)."""
    for _ in range(max_ticks):
        nav.tick()
        if nav.state != NavState.RUNNING:
            break
    return nav.state


# ─── Test 1: Full mission completes ───────────────────────────────────

def test_full_simulated_mission_reaches_complete():
    """
    Track passes through / within 2.5 m of all 3 waypoints.
    Mission must reach COMPLETE with current_waypoint_index at last index.
    """
    base_lat = 12.91686
    base_lon = 77.48698
    offset = 1e-5   # ~1.1 m per unit

    # GPS track visits waypoints in order (within 2.5 m of each)
    track = [
        {"lat": base_lat,              "lon": base_lon,              "timestamp_offset": 0},
        {"lat": base_lat + offset,     "lon": base_lon,              "timestamp_offset": 1},
        {"lat": base_lat + 2 * offset, "lon": base_lon,              "timestamp_offset": 2},
        {"lat": base_lat + 3 * offset, "lon": base_lon,              "timestamp_offset": 3},
    ]

    waypoints = [
        {"lat": base_lat + offset,     "lon": base_lon},
        {"lat": base_lat + 2 * offset, "lon": base_lon},
        {"lat": base_lat + 3 * offset, "lon": base_lon},
    ]

    nav, motors, track_path = _make_nav_with_track(track, waypoints)
    try:
        # Set GCS so geofence check doesn't immediately breach (GCS = starting point)
        nav.gcs_lat = base_lat
        nav.gcs_lon = base_lon

        nav.cmd_start()
        assert nav.state == NavState.RUNNING

        # Run ticks, advancing sim time each tick
        for tick_i in range(200):
            # Move simulated time forward ~1 s per tick so GPS progresses
            nav._gps._start_time -= 1.0 / 3.0   # 3 Hz ticks ~ 0.33 s each
            nav.tick()
            if nav.state != NavState.RUNNING:
                break

        assert nav.state == NavState.COMPLETE, f"Final state: {nav.state}"
        assert nav.current_waypoint_index == len(waypoints)
    finally:
        os.unlink(track_path)


# ─── Test 2: Geofence breach stops mission ────────────────────────────

def test_geofence_breach_stops_mission_mid_run():
    """
    GPS track walks outside 150 m geofence before reaching final waypoint.
    Final state must be GEOFENCE_STOP; motor driver must have received stop().
    """
    gcs_lat = 12.91686
    gcs_lon = 77.48698

    # Walk 200 m north (outside 150 m geofence)
    far_lat = gcs_lat + 200.0 / 111320.0

    track = [
        {"lat": gcs_lat,  "lon": gcs_lon,  "timestamp_offset": 0},
        {"lat": far_lat,  "lon": gcs_lon,  "timestamp_offset": 5},
    ]

    waypoints = [
        {"lat": far_lat + 0.001, "lon": gcs_lon},  # beyond the geofence — never reached
    ]

    nav, motors, track_path = _make_nav_with_track(track, waypoints)
    try:
        nav.gcs_lat = gcs_lat
        nav.gcs_lon = gcs_lon

        nav.cmd_start()

        # Advance sim so GPS moves toward the far point quickly
        for _ in range(100):
            nav._gps._start_time -= 0.5   # advance time 0.5 s each tick
            nav.tick()
            if nav.state != NavState.RUNNING:
                break

        assert nav.state == NavState.GEOFENCE_STOP, f"Final state: {nav.state}"
        assert motors.stop_call_count >= 1
    finally:
        os.unlink(track_path)


# ─── Test 3: GPS loss stops mission ───────────────────────────────────

def test_gps_loss_stops_mission():
    """
    After GPS returns fix_quality=0 for >5 s, the bot must transition to GPS_LOST.
    """
    base_lat = 12.91686
    base_lon = 77.48698

    # GPS driver that always returns quality=0 (no fix)
    stale_fix = FixResult(
        lat=base_lat, lon=base_lon, fix_quality=0, satellites=0,
        timestamp=time.time() - 10.0,   # stale
    )

    gps = MagicMock(spec=SimulatedGpsDriver)
    gps.get_fix.return_value = stale_fix

    compass = MagicMock(spec=CompassDriver)
    compass.get_heading.side_effect = OSError("no compass")

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

    nav.load_mission([{"lat": base_lat + 0.01, "lon": base_lon}])
    nav.cmd_start()

    # Make the watchdog think the last feed was 6 s ago (already expired at start)
    nav._watchdog._set_last_feed(time.monotonic() - 6.0)

    final_state = _run_nav_until_done(nav, max_ticks=10)

    assert final_state == NavState.GPS_LOST, f"Final state: {final_state}"
    assert motors.stop_call_count >= 1
