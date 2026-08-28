"""
tests/test_motor_driver.py — Unit tests for motor_driver.py

No GPIO required; MockMotorDriver is tested directly.
GpioMotorDriver is import-tested only (gpiozero/lgpio are stubbed).
"""

from unittest.mock import MagicMock, patch

import pytest

from motor_driver import MockMotorDriver
from navigation import compute_wheel_powers
from logger import NavLogger


# ─── MockMotorDriver basic behaviour ─────────────────────────────────

def test_mock_motor_driver_logs_values(capsys):
    """set_left/set_right must print and forward to logger."""
    mock_logger = MagicMock(spec=NavLogger)
    driver = MockMotorDriver(logger=mock_logger)

    driver.set_left(15)
    driver.set_right(20)

    # Check printed output
    captured = capsys.readouterr()
    assert "left=15" in captured.out
    assert "right=20" in captured.out

    # Logger must have been called for each set_* call
    assert mock_logger.log_event.call_count >= 2

    # Check history recording
    assert 15 in driver.left_history
    assert 20 in driver.right_history


def test_mock_motor_stop_resets_values(capsys):
    """stop() sets both to 0 and increments stop_call_count."""
    driver = MockMotorDriver()
    driver.set_left(20)
    driver.set_right(25)

    driver.stop()

    assert driver.stop_call_count == 1
    assert driver._left == 0
    assert driver._right == 0


def test_mock_motor_clamps_internally():
    """MockMotorDriver clamps power to -100..100."""
    driver = MockMotorDriver()
    driver.set_left(200)
    driver.set_right(-200)

    assert driver._left == 100
    assert driver._right == -100


# ─── Power clamping through compute_wheel_powers ─────────────────────

def test_power_values_out_of_range_are_clamped_before_reaching_driver():
    """
    Even if base_power + k*error would exceed ±30, compute_wheel_powers clamps.
    Final values fed to the driver must be within ±30 (the configured max_power).
    """
    MAX_POWER = 30

    # Extreme heading errors
    for heading_error in [1000, -1000, 500, -500]:
        left, right = compute_wheel_powers(
            heading_error,
            base_power=18,
            k=0.6,
            max_power=MAX_POWER,
        )
        assert -MAX_POWER <= left <= MAX_POWER, \
            f"left {left} out of ±{MAX_POWER} for heading_error={heading_error}"
        assert -MAX_POWER <= right <= MAX_POWER, \
            f"right {right} out of ±{MAX_POWER} for heading_error={heading_error}"

        # Now confirm MockMotorDriver would also clamp (defence-in-depth)
        driver = MockMotorDriver()
        driver.set_left(left)
        driver.set_right(right)
        assert -100 <= driver._left <= 100
        assert -100 <= driver._right <= 100
