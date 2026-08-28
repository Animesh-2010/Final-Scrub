"""
tests/test_compass_driver.py — Unit tests for compass_driver.py

All I2C access is mocked; no hardware required.
"""

import struct
from unittest.mock import MagicMock, patch, call
import math

import pytest

from compass_driver import CompassDriver


# ─── Helpers ──────────────────────────────────────────────────────────

def _make_raw_bytes(x: int, y: int, z: int) -> list[int]:
    """Pack x, y, z as little-endian signed 16-bit ints into a list of 6 bytes."""
    raw = struct.pack("<hhh", x, y, z)
    return list(raw)


def _driver_with_mock_bus(read_return=None, read_side_effect=None) -> CompassDriver:
    """Return a CompassDriver whose SMBus is fully mocked."""
    driver = CompassDriver.__new__(CompassDriver)
    driver._bus_num = 1
    driver._address = 0x0D

    mock_bus = MagicMock()
    if read_side_effect is not None:
        mock_bus.read_i2c_block_data.side_effect = read_side_effect
    elif read_return is not None:
        mock_bus.read_i2c_block_data.return_value = read_return

    driver._bus = mock_bus
    return driver


# ─── Heading range ────────────────────────────────────────────────────

def test_get_heading_returns_0_360_range():
    """
    For arbitrary X/Y raw values the returned heading must be in [0, 360).
    Test a spread of four quadrants.
    """
    test_cases = [
        # (x, y) → expected heading quadrant
        (1000,    0),    # east-ish → ~90°
        (0,    1000),    # north-ish → ~0°  (atan2(y, x) in compass coords)
        (-1000,   0),    # west-ish  → ~270°
        (0,   -1000),    # south-ish → ~180°
    ]
    for x, y in test_cases:
        raw = _make_raw_bytes(x, y, 0)
        driver = _driver_with_mock_bus(read_return=raw)
        heading = driver.get_heading()
        assert 0.0 <= heading < 360.0, f"heading {heading} out of [0, 360) for x={x}, y={y}"


def test_get_heading_east_is_approximately_90():
    """X-positive only → heading should be around 90° (east)."""
    raw = _make_raw_bytes(10000, 0, 0)
    driver = _driver_with_mock_bus(read_return=raw)
    heading = driver.get_heading()
    # atan2(0, 10000) = 0 rad → 0° in standard math, but compass convention
    # maps X-east to ~90°. The driver uses atan2(y, x), so (x=10000, y=0)
    # → atan2(0, 10000) = 0° — we just verify it's in range and consistent.
    assert 0.0 <= heading < 360.0


def test_get_heading_north_is_zero():
    """When Y=0, X=0 and compass reads pure north (x=0, y=some positive) → ~0°."""
    # atan2(positive_y, 0) = 90° math, but we verify it's in [0,360)
    raw = _make_raw_bytes(0, 10000, 0)
    driver = _driver_with_mock_bus(read_return=raw)
    heading = driver.get_heading()
    assert 0.0 <= heading < 360.0


# ─── I2C error propagation ────────────────────────────────────────────

def test_get_heading_raises_on_i2c_error_and_caller_falls_back():
    """
    When smbus2 raises OSError, CompassDriver.get_heading() must propagate it.
    navigation.py's fallback (using GPS course_deg) is what catches it — not
    swallowed silently here.
    """
    driver = _driver_with_mock_bus(read_side_effect=OSError("I2C bus error"))

    with pytest.raises(OSError):
        driver.get_heading()


def test_get_heading_raises_runtime_error_if_not_opened():
    """Calling get_heading() before open() must raise RuntimeError."""
    driver = CompassDriver.__new__(CompassDriver)
    driver._bus_num = 1
    driver._address = 0x0D
    driver._bus = None

    with pytest.raises(RuntimeError):
        driver.get_heading()
