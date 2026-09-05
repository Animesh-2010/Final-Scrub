"""
compass_driver.py — QMC5883L I2C compass driver for Lake Bot nav_service.

Chip: QMC5883L at I2C address 0x0D on bus 1.
Datasheet register map:
  0x09 = Control Register 1 (set to 0x1D for continuous mode, 200Hz ODR, 8G range, 512 OSR)
  0x00–0x05 = X_LSB, X_MSB, Y_LSB, Y_MSB, Z_LSB, Z_MSB (little-endian 16-bit signed)

CompassDriver.get_heading() returns degrees [0, 360), 0 = magnetic north.
On OSError (I2C failure), the exception propagates — navigation.py handles fallback.
"""

from __future__ import annotations

import struct

import smbus2


# QMC5883L register addresses
_REG_DATA_OUT_X_LSB = 0x00      # first byte of 6-byte data block
_REG_CONTROL1 = 0x09
_REG_SET_RESET = 0x0B           # recommended initialization register

# Control register 1 value: continuous mode | 200 Hz ODR | 8G range | 512 OSR
_CTRL1_VALUE = 0x1D


class CompassDriver:
    """
    Hardware I2C driver for the QMC5883L magnetometer.

    Usage::

        compass = CompassDriver(bus=1, address=0x0D)
        compass.open()
        heading = compass.get_heading()
    """

    def __init__(self, bus: int = 1, address: int = 0x0D):
        self._bus_num = bus
        self._address = address
        self._bus: smbus2.SMBus | None = None

    def open(self) -> None:
        """Open the I2C bus and configure the chip."""
        self._bus = smbus2.SMBus(self._bus_num)
        # Recommended initialization sequence from QMC5883L application note
        self._bus.write_byte_data(self._address, _REG_SET_RESET, 0x01)
        self._bus.write_byte_data(self._address, _REG_CONTROL1, _CTRL1_VALUE)

    def close(self) -> None:
        if self._bus is not None:
            self._bus.close()
            self._bus = None

    def get_heading(self) -> float:
        """
        Read X/Y magnetometer axes and compute heading in degrees [0, 360).
        0° = magnetic north.

        Raises:
            OSError: if the I2C read fails (device absent, bus error).
            RuntimeError: if open() has not been called.
        """
        if self._bus is None:
            raise RuntimeError("CompassDriver not open. Call open() first.")

        # Read 6 bytes starting at register 0x00 (X_LSB … Z_MSB)
        data = self._bus.read_i2c_block_data(self._address, _REG_DATA_OUT_X_LSB, 6)

        # Unpack as three signed 16-bit little-endian integers
        x, y, _z = struct.unpack_from("<hhh", bytes(data))

        # Compute heading: atan2(Y, X), convert to degrees, normalise
        import math
        heading_rad = math.atan2(float(y), float(x))
        heading_deg = math.degrees(heading_rad)
        heading_deg = (heading_deg + 360) % 360
        return heading_deg
