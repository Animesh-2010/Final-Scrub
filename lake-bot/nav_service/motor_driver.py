"""
motor_driver.py — Motor driver abstraction for Lake Bot nav_service.

GPIO backend: gpiozero with lgpio pin factory (required for Pi 5 / RP1 chip).
Motor driver IC: L298N dual H-bridge.

Pin assignments (BCM numbering):
  Left  motor: PWM=12, DIR_A=5,  DIR_B=6
  Right motor: PWM=13, DIR_A=16, DIR_B=20

Motor power: integer in range -100..100.
  positive = forward  (DIR_A high, DIR_B low)
  negative = reverse  (DIR_A low,  DIR_B high)
  zero     = coast    (both DIR pins low)

GpioMotorDriver  — real hardware, uses gpiozero + lgpio.
MockMotorDriver  — no GPIO; prints values and forwards to logger.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logger import NavLogger


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class MotorDriver(ABC):
    """Abstract interface for left/right paddle-wheel motors."""

    @abstractmethod
    def set_left(self, power: int) -> None:
        """Set left motor power. power: -100..100 (positive = forward)."""

    @abstractmethod
    def set_right(self, power: int) -> None:
        """Set right motor power. power: -100..100 (positive = forward)."""

    @abstractmethod
    def stop(self) -> None:
        """Immediately stop both motors (coast)."""


# ---------------------------------------------------------------------------
# GPIO driver (Pi 5 + lgpio)
# ---------------------------------------------------------------------------

def _get_gpio_motor_driver_class():
    """
    Factory that imports gpiozero at call-time and sets the lgpio pin factory.
    Deferred to avoid ImportError on non-Pi machines during testing.
    """
    from gpiozero import Device, PWMOutputDevice, DigitalOutputDevice
    from gpiozero.pins.lgpio import LGPIOFactory

    # Set lgpio as the global default pin factory for all gpiozero objects
    Device.pin_factory = LGPIOFactory()

    class _GpioMotorDriver(MotorDriver):
        """
        Real GPIO motor driver using gpiozero + lgpio.

        Uses PWMOutputDevice for speed control and DigitalOutputDevice
        pairs for direction (L298N H-bridge convention).
        """

        def __init__(
            self,
            left_pwm_pin: int = 12,
            left_dir_a_pin: int = 5,
            left_dir_b_pin: int = 6,
            right_pwm_pin: int = 13,
            right_dir_a_pin: int = 16,
            right_dir_b_pin: int = 20,
            pwm_frequency: int = 1000,
            max_power: int = 100,
        ):
            self._max_power = max_power
            self._left_pwm = PWMOutputDevice(left_pwm_pin, frequency=pwm_frequency)
            self._left_dir_a = DigitalOutputDevice(left_dir_a_pin)
            self._left_dir_b = DigitalOutputDevice(left_dir_b_pin)

            self._right_pwm = PWMOutputDevice(right_pwm_pin, frequency=pwm_frequency)
            self._right_dir_a = DigitalOutputDevice(right_dir_a_pin)
            self._right_dir_b = DigitalOutputDevice(right_dir_b_pin)

        # ------------------------------------------------------------------

        def _apply(
            self,
            pwm: "PWMOutputDevice",
            dir_a: "DigitalOutputDevice",
            dir_b: "DigitalOutputDevice",
            power: int,
        ) -> None:
            power = max(-self._max_power, min(self._max_power, power))
            if power == 0:
                dir_a.off()
                dir_b.off()
                pwm.value = 0.0
            elif power > 0:
                dir_a.on()
                dir_b.off()
                pwm.value = power / 100.0
            else:
                dir_a.off()
                dir_b.on()
                pwm.value = abs(power) / 100.0

        def set_left(self, power: int) -> None:
            self._apply(self._left_pwm, self._left_dir_a, self._left_dir_b, power)

        def set_right(self, power: int) -> None:
            self._apply(self._right_pwm, self._right_dir_a, self._right_dir_b, power)

        def stop(self) -> None:
            self.set_left(0)
            self.set_right(0)

        def close(self) -> None:
            self.stop()
            self._left_pwm.close()
            self._left_dir_a.close()
            self._left_dir_b.close()
            self._right_pwm.close()
            self._right_dir_a.close()
            self._right_dir_b.close()

    return _GpioMotorDriver


class GpioMotorDriver(MotorDriver):
    """
    Real GPIO motor driver — thin wrapper that defers gpiozero import until
    construction, so this module can be imported on non-Pi machines without error.
    """

    def __init__(
        self,
        left_pwm_pin: int = 12,
        left_dir_a_pin: int = 5,
        left_dir_b_pin: int = 6,
        right_pwm_pin: int = 13,
        right_dir_a_pin: int = 16,
        right_dir_b_pin: int = 20,
        pwm_frequency: int = 1000,
        max_power: int = 100,
    ):
        cls = _get_gpio_motor_driver_class()
        self._impl = cls(
            left_pwm_pin=left_pwm_pin,
            left_dir_a_pin=left_dir_a_pin,
            left_dir_b_pin=left_dir_b_pin,
            right_pwm_pin=right_pwm_pin,
            right_dir_a_pin=right_dir_a_pin,
            right_dir_b_pin=right_dir_b_pin,
            pwm_frequency=pwm_frequency,
            max_power=max_power,
        )

    def set_left(self, power: int) -> None:
        self._impl.set_left(power)

    def set_right(self, power: int) -> None:
        self._impl.set_right(power)

    def stop(self) -> None:
        self._impl.stop()

    def close(self) -> None:
        self._impl.close()


# ---------------------------------------------------------------------------
# Mock driver (no GPIO)
# ---------------------------------------------------------------------------

class MockMotorDriver(MotorDriver):
    """
    Software-only motor driver for bench testing and simulation.

    On every set_left / set_right call:
      - Prints: [MOCK MOTOR] left=<n> right=<n> t=<timestamp>
      - Forwards values to NavLogger if one is supplied.

    stop() sets both to 0.
    """

    def __init__(self, logger: "NavLogger | None" = None, max_power: int = 100):
        self._logger = logger
        self._max_power = max_power
        self._left: int = 0
        self._right: int = 0
        # Track calls for test assertions
        self.stop_call_count: int = 0
        self.left_history: list[int] = []
        self.right_history: list[int] = []

    def _report(self) -> None:
        ts = time.time()
        print(f"[MOCK MOTOR] left={self._left} right={self._right} t={ts:.3f}")
        if self._logger is not None:
            self._logger.log_event(
                "motor_update",
                f"left={self._left} right={self._right}",
                timestamp=ts,
            )

    def set_left(self, power: int) -> None:
        self._left = max(-self._max_power, min(self._max_power, power))
        self.left_history.append(self._left)
        self._report()

    def set_right(self, power: int) -> None:
        self._right = max(-self._max_power, min(self._max_power, power))
        self.right_history.append(self._right)
        self._report()

    def stop(self) -> None:
        self.stop_call_count += 1
        self._left = 0
        self._right = 0
        self._report()

    def close(self) -> None:
        self.stop()
