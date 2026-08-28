"""
conftest.py — Top-level pytest configuration.

Patches all hardware-dependent imports (gpiozero, lgpio, smbus2, serial)
so that every test module can import nav_service modules without physical
hardware attached. The patches are installed before any test collection.
"""

import sys
import types
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Add nav_service to the import path so tests can import its modules directly
# ---------------------------------------------------------------------------
import os
NAV_SERVICE = os.path.join(os.path.dirname(__file__), "nav_service")
if NAV_SERVICE not in sys.path:
    sys.path.insert(0, NAV_SERVICE)


# ---------------------------------------------------------------------------
# Stub out hardware libraries before any module-level imports run
# ---------------------------------------------------------------------------

def _make_module(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


# --- lgpio ------------------------------------------------------------------
lgpio_mod = _make_module("lgpio")

# --- gpiozero ---------------------------------------------------------------
gpiozero_mod = _make_module("gpiozero")

class _FakePWMOutputDevice:
    def __init__(self, *a, **kw):
        self.value = 0.0
        self.frequency = kw.get("frequency", 1000)
    def on(self): self.value = 1.0
    def off(self): self.value = 0.0
    def close(self): pass

class _FakeDigitalOutputDevice:
    def __init__(self, *a, **kw):
        self.value = False
    def on(self):  self.value = True
    def off(self): self.value = False
    def close(self): pass

class _FakeDevice:
    pin_factory = None

gpiozero_mod.PWMOutputDevice = _FakePWMOutputDevice
gpiozero_mod.DigitalOutputDevice = _FakeDigitalOutputDevice
gpiozero_mod.Device = _FakeDevice

gpiozero_pins_mod = _make_module("gpiozero.pins")
gpiozero_lgpio_mod = _make_module("gpiozero.pins.lgpio")

class _FakeLGPIOFactory:
    pass

gpiozero_lgpio_mod.LGPIOFactory = _FakeLGPIOFactory

# --- smbus2 -----------------------------------------------------------------
smbus2_mod = _make_module("smbus2")

class _FakeSMBus:
    def __init__(self, *a, **kw): pass
    def write_byte_data(self, *a): pass
    def read_i2c_block_data(self, addr, reg, length):
        # Return neutral bytes (heading = 0°)
        return [0] * length
    def close(self): pass

smbus2_mod.SMBus = _FakeSMBus

# --- serial -----------------------------------------------------------------
serial_mod = _make_module("serial")

class _FakeSerial:
    def __init__(self, *a, **kw):
        self.is_open = True
        self._lines = []
    def readline(self):
        return b""
    def close(self):
        self.is_open = False

serial_mod.Serial = _FakeSerial

class _FakeSerialException(Exception):
    pass

serial_mod.SerialException = _FakeSerialException

# --- pynmea2 ----------------------------------------------------------------
# Real library should be importable in the test environment (pure Python, no HW)
# If not installed, stub it out minimally
try:
    import pynmea2  # noqa: F401
except ImportError:
    pynmea2_mod = _make_module("pynmea2")
    pynmea2_mod.parse = MagicMock(side_effect=Exception("pynmea2 not installed"))
    pynmea2_mod.ParseError = Exception
    pynmea2_mod.ChecksumError = Exception

# --- websockets -------------------------------------------------------------
# Stub only if not installed (it usually is for dashboard tests)
try:
    import websockets  # noqa: F401
except ImportError:
    ws_mod = _make_module("websockets")
    ws_exceptions_mod = _make_module("websockets.exceptions")
    ws_exceptions_mod.ConnectionClosed = Exception
    ws_mod.serve = MagicMock()
    ws_mod.exceptions = ws_exceptions_mod

# --- yaml -------------------------------------------------------------------
try:
    import yaml  # noqa: F401
except ImportError:
    yaml_mod = _make_module("yaml")
    yaml_mod.safe_load = MagicMock(return_value={})
