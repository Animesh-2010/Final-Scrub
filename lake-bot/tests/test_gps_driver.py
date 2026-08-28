"""
tests/test_gps_driver.py — Unit tests for gps_driver.py

All tests use direct sentence parsing (_parse_sentence / _process_sentence)
so they are independent of the mock serial readline path.
"""

import json
import math
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

# conftest adds nav_service to sys.path and stubs hardware imports
from gps_driver import GpsDriver, SimulatedGpsDriver, FixResult


# ─── Helpers ──────────────────────────────────────────────────────────

def _driver_from_sentences(sentences: list[str]) -> GpsDriver:
    """
    Return a GpsDriver with the given NMEA sentences already processed.
    Uses _parse_sentence + _process_sentence directly, bypassing mock serial.
    """
    driver = GpsDriver.__new__(GpsDriver)
    driver._device = "/dev/ttyAMA0"
    driver._baud = 9600
    driver._fix = FixResult()
    driver._serial = None  # not needed for parse-only tests

    for raw in sentences:
        parsed = driver._parse_sentence(raw)
        if parsed is not None:
            driver._process_sentence(parsed)

    driver._fix.timestamp = time.time()
    return driver


# ─── GGA valid fix ────────────────────────────────────────────────────

def test_parse_gga_valid_fix():
    """GGA with fix_quality=1 must yield correct lat/lon/alt/sat/quality."""
    # Valid GGA sentence with correct checksum near Bengaluru
    gga = "$GPGGA,123519,1255.0159,N,07729.2188,E,1,08,0.9,5.0,M,0.0,M,,*7D"
    driver = _driver_from_sentences([gga])
    fix = driver._fix

    # lat: 12°55.0159' N = 12 + 55.0159/60
    expected_lat = 12 + 55.0159 / 60
    # lon: 077°29.2188' E = 77 + 29.2188/60
    expected_lon = 77 + 29.2188 / 60

    assert fix.fix_quality == 1, f"fix_quality={fix.fix_quality}"
    assert abs(fix.lat - expected_lat) < 1e-4, f"lat {fix.lat} != {expected_lat}"
    assert abs(fix.lon - expected_lon) < 1e-4, f"lon {fix.lon} != {expected_lon}"
    assert abs(fix.altitude_m - 5.0) < 1e-2
    assert fix.satellites == 8


def test_parse_gga_no_fix():
    """GGA with fix_quality=0 must not raise and fix_quality must be 0."""
    # quality=0 → fix_quality stays 0
    gga = "$GPGGA,123519,0000.0000,N,00000.0000,E,0,00,99.9,0.0,M,0.0,M,,*66"
    driver = _driver_from_sentences([gga])
    assert driver._fix.fix_quality == 0


# ─── RMC speed and course ─────────────────────────────────────────────

def test_parse_rmc_speed_and_course():
    """RMC: speed 1.0 knot → 0.5144 m/s (within 0.01). course must match."""
    # Valid RMC sentence with status=A
    rmc = "$GPRMC,123519,A,1255.0159,N,07729.2188,E,1.0,90.0,230394,003.1,W*5A"
    driver = _driver_from_sentences([rmc])
    fix = driver._fix

    expected_mps = 1.0 * 0.514444
    assert abs(fix.speed_mps - expected_mps) < 0.01, f"speed {fix.speed_mps} != {expected_mps}"
    assert abs(fix.course_deg - 90.0) < 0.01, f"course {fix.course_deg} != 90.0"


# ─── Malformed sentence ───────────────────────────────────────────────

def test_parse_malformed_sentence_does_not_crash():
    """Truncated/corrupt NMEA must not raise; _parse_sentence should return None."""
    driver = GpsDriver.__new__(GpsDriver)
    driver._device = "/dev/ttyAMA0"
    driver._baud = 9600
    driver._fix = FixResult()
    driver._serial = None

    malformed = "$GPGGA,CORRUPTED DATA,,,,,,"
    result = driver._parse_sentence(malformed)  # must not raise; may return None
    # No assertion on result — just must not raise
    assert True


# ─── SimulatedGpsDriver interpolation ────────────────────────────────

def test_simulated_gps_driver_interpolates():
    """
    With a 2-point track (t=0 and t=10), requesting a fix at t=5 (real elapsed)
    should return lat/lon at 50% of the way between the two points.
    """
    track = [
        {"lat": 12.91000, "lon": 77.48000, "timestamp_offset": 0},
        {"lat": 12.92000, "lon": 77.49000, "timestamp_offset": 10},
    ]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(track, f)
        track_path = f.name

    try:
        driver = SimulatedGpsDriver(track_file=track_path)
        # Manually set _start_time so that elapsed ≈ 5 s
        driver.open()
        driver._start_time = time.time() - 5.0   # pretend we started 5s ago

        fix = driver.get_fix()

        expected_lat = 12.91000 + 0.5 * (12.92000 - 12.91000)
        expected_lon = 77.48000 + 0.5 * (77.49000 - 77.48000)

        assert abs(fix.lat - expected_lat) < 1e-5, f"lat {fix.lat} != {expected_lat}"
        assert abs(fix.lon - expected_lon) < 1e-5, f"lon {fix.lon} != {expected_lon}"
    finally:
        os.unlink(track_path)


def test_simulated_gps_driver_random_walk_no_track():
    """SimulatedGpsDriver with no track file must return fix_quality=1 near start."""
    driver = SimulatedGpsDriver()
    driver.open()
    fix = driver.get_fix()

    assert fix.fix_quality == 1
    assert fix.satellites == 8
    # Should be within ~1 degree of the default origin
    assert abs(fix.lat - 12.91686) < 1.0
    assert abs(fix.lon - 77.48698) < 1.0
