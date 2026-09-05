"""
tests/test_safety.py — Unit tests for safety.py

Tests: geofence, GPS watchdog, mission timer.
Time is injected via private _set_ helpers — no monkeypatching of time.time().
"""

import time

import pytest

from safety import GpsWatchdog, MissionTimer, check_geofence


# ─── Geofence ─────────────────────────────────────────────────────────

def test_geofence_within_radius_ok():
    """50 m from GCS with 150 m radius → True (safe)."""
    gcs_lat, gcs_lon = 12.91686, 77.48698

    # Move ~50 m north of GCS (0.00045° lat ≈ 50 m)
    current_lat = gcs_lat + 50.0 / 111320.0
    current_lon = gcs_lon

    result = check_geofence(current_lat, current_lon, gcs_lat, gcs_lon, radius_m=150.0)
    assert result is True, "Should be within geofence"


def test_geofence_breach_beyond_radius():
    """200 m from GCS with 150 m radius → False (breach)."""
    gcs_lat, gcs_lon = 12.91686, 77.48698

    # Move ~200 m north
    current_lat = gcs_lat + 200.0 / 111320.0
    current_lon = gcs_lon

    result = check_geofence(current_lat, current_lon, gcs_lat, gcs_lon, radius_m=150.0)
    assert result is False, "Should be outside geofence"


def test_geofence_exactly_at_gcs():
    """Bot at GCS position → True (inside, distance=0)."""
    gcs_lat, gcs_lon = 12.91686, 77.48698
    result = check_geofence(gcs_lat, gcs_lon, gcs_lat, gcs_lon, radius_m=150.0)
    assert result is True


# ─── GpsWatchdog ──────────────────────────────────────────────────────

def test_gps_watchdog_not_expired_after_feed():
    """Immediately after feed(), expired() must be False."""
    watchdog = GpsWatchdog(timeout_s=5.0)
    watchdog.feed()
    assert watchdog.expired() is False


def test_gps_watchdog_expires_after_timeout():
    """Set last_feed to 6 seconds ago → expired() must be True."""
    watchdog = GpsWatchdog(timeout_s=5.0)
    watchdog._set_last_feed(time.monotonic() - 6.0)   # 6 s ago
    assert watchdog.expired() is True


def test_gps_watchdog_not_expired_within_timeout():
    """Set last_feed to 3 seconds ago (< 5 s) → expired() must be False."""
    watchdog = GpsWatchdog(timeout_s=5.0)
    watchdog._set_last_feed(time.monotonic() - 3.0)
    assert watchdog.expired() is False


def test_gps_watchdog_feed_resets_timer():
    """Feed after expiry → expired() becomes False again."""
    watchdog = GpsWatchdog(timeout_s=5.0)
    watchdog._set_last_feed(time.monotonic() - 10.0)
    assert watchdog.expired() is True
    watchdog.feed()
    assert watchdog.expired() is False


# ─── MissionTimer ─────────────────────────────────────────────────────

def test_mission_timer_not_started():
    """Timer not started → expired() is False."""
    timer = MissionTimer(max_runtime_s=1800.0)
    assert timer.expired() is False


def test_mission_timer_expires_after_max_runtime():
    """Inject start_time 1801 s ago → expired() must be True."""
    timer = MissionTimer(max_runtime_s=1800.0)
    timer._set_start_time(time.monotonic() - 1801.0)
    assert timer.expired() is True


def test_mission_timer_not_expired_within_max_runtime():
    """Inject start_time 900 s ago → expired() must be False."""
    timer = MissionTimer(max_runtime_s=1800.0)
    timer._set_start_time(time.monotonic() - 900.0)
    assert timer.expired() is False


def test_mission_timer_start_resets():
    """start() after expiry → expired() False again."""
    timer = MissionTimer(max_runtime_s=1800.0)
    timer._set_start_time(time.monotonic() - 2000.0)
    assert timer.expired() is True
    timer.start()
    assert timer.expired() is False
