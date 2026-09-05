"""
safety.py — Geofence, GPS watchdog, and mission timer for Lake Bot nav_service.

Functions and classes:
  check_geofence(current_lat, current_lon, gcs_lat, gcs_lon, radius_m) -> bool
  GpsWatchdog — feeds on valid fix; expired() after timeout_s seconds
  MissionTimer — starts on start(); expired() after max_runtime_s seconds
"""

from __future__ import annotations

import math
import time


# ---------------------------------------------------------------------------
# Haversine helper (local copy avoids circular import with navigation.py)
# ---------------------------------------------------------------------------

_EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in metres between two lat/lon points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Geofence
# ---------------------------------------------------------------------------

def check_geofence(
    current_lat: float,
    current_lon: float,
    gcs_lat: float,
    gcs_lon: float,
    radius_m: float = 150.0,
) -> bool:
    """
    Return True  (safe)  if the bot is within radius_m of the GCS.
    Return False (breach) if the bot has left the geofence.
    """
    distance = _haversine_m(current_lat, current_lon, gcs_lat, gcs_lon)
    return distance <= radius_m


# ---------------------------------------------------------------------------
# GPS watchdog
# ---------------------------------------------------------------------------

class GpsWatchdog:
    """
    Detects GPS signal loss.

    Call .feed() every time a valid fix is received.
    Call .expired() to check whether more than timeout_s seconds have
    passed since the last feed.
    """

    def __init__(self, timeout_s: float = 5.0):
        self._timeout_s = timeout_s
        self._last_feed: float = time.monotonic()

    def feed(self) -> None:
        """Record that a valid fix was received right now."""
        self._last_feed = time.monotonic()

    def expired(self) -> bool:
        """Return True if no fix has been received for longer than timeout_s."""
        return (time.monotonic() - self._last_feed) > self._timeout_s

    # Allow test injection of a custom time source
    def _set_last_feed(self, t: float) -> None:
        self._last_feed = t


# ---------------------------------------------------------------------------
# Mission timer
# ---------------------------------------------------------------------------

class MissionTimer:
    """
    Enforces maximum mission duration.

    Call .start() when the mission begins.
    Call .expired() to check whether max_runtime_s seconds have elapsed.
    """

    def __init__(self, max_runtime_s: float = 1800.0):
        self._max_runtime_s = max_runtime_s
        self._start_time: float | None = None

    def start(self) -> None:
        """Start (or restart) the mission timer."""
        self._start_time = time.monotonic()

    def expired(self) -> bool:
        """Return True if the mission has been running longer than max_runtime_s."""
        if self._start_time is None:
            return False
        return (time.monotonic() - self._start_time) > self._max_runtime_s

    # Allow test injection
    def _set_start_time(self, t: float) -> None:
        self._start_time = t
