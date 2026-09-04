"""
arduino_link.py — UART links to dual Arduino boards for SCRUB v4 nav_service.

Topology options:
  A) Dual-board (original design):
     1. SensorGpsLink  — NEO-M8N GPS + compass + analog sensors (read-only)
        Connected via Pi GPIO hardware UART (/dev/ttyAMA0).
     2. MotorRcLink    — TB6600 motor drivers + FlySky RC receiver (read + write)
        Connected via USB (/dev/ttyACM0 or similar).

  B) Single-board (differential drive):
     1. SensorGpsLink  — all sensors + GPS + compass + motor control
        Connected via Pi GPIO UART (/dev/ttyAMA0) or USB.
        Motor commands sent back over the same link.

Arduino -> Pi packet (parsed into ArduinoState):
  {"seq": int, "gps": {...}, "hdg": float,
   "compass": {"x": int, "y": int, "z": int},
   "sensors": {...}, "mode": "AUTO"|"MANUAL"}

GPS format: {"lat", "lon", "alt", "spd", "course", "sats_view", "sats_used", "fix"}

Pi -> Arduino commands (sent via SensorGpsLink or MotorRcLink):
  {"cmd": "motor", "l": int, "r": int}   (l/r in -100..100)
  {"cmd": "ping"}
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ArduinoState:
    """Parsed snapshot from the most recent Arduino telemetry line."""
    seq: int = 0
    gps_lat: float = 0.0
    gps_lon: float = 0.0
    gps_alt: float = 0.0
    gps_spd: float = 0.0
    gps_course: float = 0.0
    gps_sats: int = 0          # total satellites (backward compat)
    gps_sats_view: int = 0     # satellites in view
    gps_sats_used: int = 0     # satellites used in fix
    gps_fix: int = 0
    heading: float = 0.0       # compass heading in degrees
    compass_x: int = 0         # raw magnetometer X
    compass_y: int = 0         # raw magnetometer Y
    compass_z: int = 0         # raw magnetometer Z
    sensors: dict[str, float] = field(default_factory=dict)
    mode: str = "MANUAL"
    rc_ch1: int = 1500
    rc_ch2: int = 1500
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# Sensor+GPS link (read-only, GPIO UART)
# ---------------------------------------------------------------------------

class SensorGpsLink:
    """
    Link to the sensor+GPS Arduino (read + write for single-board setups).

    Reads newline-delimited JSON from /dev/ttyAMA0 (or USB) containing:
      GPS coordinates, compass heading (hdg + raw XYZ), analog sensor values,
      and mode.  Supports sending motor commands and pings back over the
      same UART link for single-board differential-drive configurations.
    """

    def __init__(
        self,
        device: str = "/dev/ttyAMA0",
        baud: int = 115200,
        reconnect_backoff_s: float = 2.0,
        sensor_keys: list[str] | None = None,
        staleness_timeout_s: float = 3.0,
    ):
        self._device = device
        self._baud = baud
        self._reconnect_backoff_s = reconnect_backoff_s
        self._sensor_keys = sensor_keys or ["ph", "tds", "turb"]
        self._staleness_timeout_s = staleness_timeout_s
        self._serial = None
        self._latest = ArduinoState()
        self._lock = threading.Lock()
        self._running = False
        self._reader_thread: threading.Thread | None = None
        self._last_valid_time: float = 0.0
        self._was_fresh: bool = True

    def open(self) -> None:
        """Open the serial port and start the background reader."""
        import serial
        try:
            self._serial = serial.Serial(self._device, self._baud, timeout=1)
        except Exception as exc:
            log.warning(f"SensorGpsLink: could not open {self._device} — sensor+GPS link disabled: {exc}")
            return
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True, name="sensor-gps-reader")
        self._reader_thread.start()
        log.info(f"SensorGpsLink opened on {self._device} @ {self._baud}")

    def _read_loop(self) -> None:
        """Background thread: read lines, parse, update _latest."""
        while self._running:
            try:
                raw = self._serial.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="ignore").strip()
                if not line:
                    continue
                self._parse_line(line)
            except Exception as exc:
                log.warning(f"SensorGpsLink serial read error: {exc}")
                time.sleep(self._reconnect_backoff_s)

    def _parse_line(self, line: str) -> None:
        """Parse one JSON line, extracting GPS, heading, compass, and sensor fields."""
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.debug(f"SensorGpsLink: malformed JSON: {line!r}")
            return

        try:
            gps = obj.get("gps", {})
            sensors = obj.get("sensors", {})
            compass = obj.get("compass", {})

            # Handle both new format (sats_view/sats_used) and legacy (sats)
            sats_view = int(gps.get("sats_view", 0))
            sats_used = int(gps.get("sats_used", 0))
            sats_legacy = int(gps.get("sats", 0))
            # Prefer new fields; fall back to legacy total
            sats_total = sats_used if sats_used else sats_legacy

            state = ArduinoState(
                seq=int(obj.get("seq", 0)),
                gps_lat=float(gps.get("lat", 0.0)),
                gps_lon=float(gps.get("lon", 0.0)),
                gps_alt=float(gps.get("alt", 0.0)),
                gps_spd=float(gps.get("spd", 0.0)),
                gps_course=float(gps.get("course", 0.0)),
                gps_sats=sats_total,
                gps_sats_view=sats_view,
                gps_sats_used=sats_used,
                gps_fix=int(gps.get("fix", 0)),
                heading=float(obj.get("hdg", 0.0)),
                compass_x=int(compass.get("x", 0)),
                compass_y=int(compass.get("y", 0)),
                compass_z=int(compass.get("z", 0)),
                sensors={k: float(sensors.get(k, 0.0)) for k in self._sensor_keys},
                timestamp=time.time(),
            )

            with self._lock:
                self._latest = state
                self._last_valid_time = time.time()

            log.debug(f"[UART RX] {line!r}")

        except (KeyError, ValueError, TypeError) as exc:
            log.debug(f"SensorGpsLink: parse error: {exc} in {line!r}")

    def get_latest_state(self) -> ArduinoState:
        """Return the most recent parsed packet (never blocks)."""
        with self._lock:
            return self._latest

    def is_stale(self) -> bool:
        """Return True if no valid packet received within staleness window."""
        if self._last_valid_time == 0.0:
            return True
        return (time.time() - self._last_valid_time) > self._staleness_timeout_s

    def check_staleness_transition(self) -> None:
        """Log fresh<->stale transitions for diagnostics."""
        stale = self.is_stale()
        if self._was_fresh and stale:
            log.warning(f"SensorGpsLink: STALE — no valid packet for {self._staleness_timeout_s}s")
        elif not self._was_fresh and not stale:
            log.info("SensorGpsLink: RECOVERED — valid packets resuming")
        self._was_fresh = not stale

    def send_motor_command(self, left: int, right: int) -> None:
        """Send a motor power command to the Arduino (single-board mode)."""
        left = max(-100, min(100, int(left)))
        right = max(-100, min(100, int(right)))
        msg = json.dumps({"cmd": "motor", "l": left, "r": right}) + "\n"
        log.debug(f"[UART TX] {msg.strip()!r}")
        self._write(msg)

    def send_ping(self) -> None:
        """Send a heartbeat/no-op to keep the link alive."""
        msg = json.dumps({"cmd": "ping"}) + "\n"
        log.debug(f"[UART TX] {msg.strip()!r}")
        self._write(msg)

    def _write(self, data: str) -> None:
        """Write raw bytes to serial; log and ignore on error."""
        if self._serial is None or not self._serial.is_open:
            return
        try:
            self._serial.write(data.encode("ascii"))
        except Exception as exc:
            log.warning(f"SensorGpsLink write error: {exc}")

    def close(self) -> None:
        """Stop the reader thread and close the serial port."""
        self._running = False
        if self._reader_thread:
            self._reader_thread.join(timeout=3.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        log.info("SensorGpsLink closed")


# ---------------------------------------------------------------------------
# Motor+RC link (read/write, USB)
# ---------------------------------------------------------------------------

class MotorRcLink:
    """
    Read/write link to the motor+RC Arduino.

    Reads newline-delimited JSON from USB containing:
      RC channel values and physical mode switch state.
    Writes motor commands and ping heartbeats.
    """

    def __init__(
        self,
        device: str = "/dev/ttyACM0",
        baud: int = 115200,
        reconnect_backoff_s: float = 2.0,
        staleness_timeout_s: float = 1.0,
    ):
        self._device = device
        self._baud = baud
        self._reconnect_backoff_s = reconnect_backoff_s
        self._staleness_timeout_s = staleness_timeout_s
        self._serial = None
        self._latest_mode: str = "MANUAL"
        self._latest_rc_ch1: int = 1500
        self._latest_rc_ch2: int = 1500
        self._latest_seq: int = 0
        self._lock = threading.Lock()
        self._running = False
        self._reader_thread: threading.Thread | None = None
        self._last_valid_time: float = 0.0
        self._was_fresh: bool = True

    def open(self) -> None:
        """Open the serial port and start the background reader."""
        import serial
        try:
            self._serial = serial.Serial(self._device, self._baud, timeout=1)
        except Exception as exc:
            log.warning(f"MotorRcLink: could not open {self._device} — motor link disabled: {exc}")
            return
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True, name="motor-rc-reader")
        self._reader_thread.start()
        log.info(f"MotorRcLink opened on {self._device} @ {self._baud}")

    def _read_loop(self) -> None:
        """Background thread: read lines, parse, update state."""
        while self._running:
            try:
                raw = self._serial.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="ignore").strip()
                if not line:
                    continue
                self._parse_line(line)
            except Exception as exc:
                log.warning(f"MotorRcLink serial read error: {exc}")
                time.sleep(self._reconnect_backoff_s)

    def _parse_line(self, line: str) -> None:
        """Parse one JSON line, extracting mode and RC channel fields."""
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.debug(f"MotorRcLink: malformed JSON: {line!r}")
            return

        try:
            rc = obj.get("rc", {})

            with self._lock:
                self._latest_mode = str(obj.get("mode", "MANUAL"))
                self._latest_rc_ch1 = int(rc.get("ch1", 1500))
                self._latest_rc_ch2 = int(rc.get("ch2", 1500))
                self._latest_seq = int(obj.get("seq", 0))
                self._last_valid_time = time.time()

            log.debug(f"[USB RX] {line!r}")

        except (KeyError, ValueError, TypeError) as exc:
            log.debug(f"MotorRcLink: parse error: {exc} in {line!r}")

    def get_mode(self) -> str:
        """Return the most recent mode (AUTO/MANUAL)."""
        with self._lock:
            return self._latest_mode

    def get_rc_channels(self) -> tuple[int, int]:
        """Return the most recent RC channel values (ch1, ch2)."""
        with self._lock:
            return self._latest_rc_ch1, self._latest_rc_ch2

    def get_seq(self) -> int:
        """Return the most recent sequence number."""
        with self._lock:
            return self._latest_seq

    def is_stale(self) -> bool:
        """Return True if no valid packet received within staleness window."""
        if self._last_valid_time == 0.0:
            return True
        return (time.time() - self._last_valid_time) > self._staleness_timeout_s

    def check_staleness_transition(self) -> None:
        """Log fresh<->stale transitions for diagnostics."""
        stale = self.is_stale()
        if self._was_fresh and stale:
            log.warning(f"MotorRcLink: STALE — no valid packet for {self._staleness_timeout_s}s")
        elif not self._was_fresh and not stale:
            log.info("MotorRcLink: RECOVERED — valid packets resuming")
        self._was_fresh = not stale

    def send_motor_command(self, left: int, right: int) -> None:
        """Send a motor power command to the Arduino."""
        left = max(-100, min(100, int(left)))
        right = max(-100, min(100, int(right)))
        msg = json.dumps({"cmd": "motor", "l": left, "r": right}) + "\n"
        log.debug(f"[USB TX] {msg.strip()!r}")
        self._write(msg)

    def send_ping(self) -> None:
        """Send a heartbeat/no-op to keep the link alive."""
        msg = json.dumps({"cmd": "ping"}) + "\n"
        log.debug(f"[USB TX] {msg.strip()!r}")
        self._write(msg)

    def _write(self, data: str) -> None:
        """Write raw bytes to serial; log and ignore on error."""
        if self._serial is None or not self._serial.is_open:
            return
        try:
            self._serial.write(data.encode("ascii"))
        except Exception as exc:
            log.warning(f"MotorRcLink write error: {exc}")

    def close(self) -> None:
        """Stop the reader thread and close the serial port."""
        self._running = False
        if self._reader_thread:
            self._reader_thread.join(timeout=3.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        log.info("MotorRcLink closed")


# ---------------------------------------------------------------------------
# Dual-board composite link
# ---------------------------------------------------------------------------

class DualArduinoLink:
    """
    Composite link merging SensorGpsLink + optional MotorRcLink into a single
    ArduinoState interface compatible with NavigationController.

    Supports two topologies:
      A) Dual-board: sensor_gps + motor_rc — GPS/heading from sensor, mode/RC from motor.
      B) Single-board: sensor_gps only (motor_rc=None) — all fields from one Arduino,
         motor commands sent back over sensor_gps UART.

    Staleness rules:
      - SensorGpsLink stale → gps_fix forced to 0 (triggers GpsWatchdog)
      - MotorRcLink stale (if present) → mode forced to MANUAL (fail-safe default)
    """

    def __init__(
        self,
        sensor_gps: SensorGpsLink,
        motor_rc: MotorRcLink | None = None,
        gps_reader: Optional[Any] = None,
    ):
        self._sensor_gps = sensor_gps
        self._motor_rc = motor_rc
        self._gps_reader = gps_reader  # optional Pi-side NMEA GPS source
        self._single_board = motor_rc is None

    def open(self) -> None:
        """Open all links."""
        self._sensor_gps.open()
        if self._motor_rc is not None:
            self._motor_rc.open()
        if self._gps_reader is not None:
            self._gps_reader.open()
        mode = "single-board" if self._single_board else "dual-board"
        log.info(f"DualArduinoLink opened ({mode})")

    def get_latest_state(self) -> ArduinoState:
        """
        Merge the sub-links into a single ArduinoState.

        Single-board mode: all fields come from SensorGpsLink.
        Dual-board mode: GPS/heading/sensors from SensorGpsLink, mode/RC from MotorRcLink.
        Staleness overrides applied here.
        """
        # Check staleness transitions (logs warnings)
        self._sensor_gps.check_staleness_transition()
        if self._motor_rc is not None:
            self._motor_rc.check_staleness_transition()

        # Get raw state from sensor link
        sensor_state = self._sensor_gps.get_latest_state()

        if self._single_board:
            # Single-board: everything from sensor link, mode is in the packet
            merged = ArduinoState(
                seq=sensor_state.seq,
                gps_lat=sensor_state.gps_lat,
                gps_lon=sensor_state.gps_lon,
                gps_alt=sensor_state.gps_alt,
                gps_spd=sensor_state.gps_spd,
                gps_course=sensor_state.gps_course,
                gps_sats=sensor_state.gps_sats,
                gps_sats_view=sensor_state.gps_sats_view,
                gps_sats_used=sensor_state.gps_sats_used,
                gps_fix=sensor_state.gps_fix,
                heading=sensor_state.heading,
                compass_x=sensor_state.compass_x,
                compass_y=sensor_state.compass_y,
                compass_z=sensor_state.compass_z,
                sensors=sensor_state.sensors,
                mode=sensor_state.mode,
                timestamp=max(sensor_state.timestamp, time.time()),
            )
        else:
            # Dual-board: merge from both links
            mode = self._motor_rc.get_mode()
            rc_ch1, rc_ch2 = self._motor_rc.get_rc_channels()
            motor_seq = self._motor_rc.get_seq()

            merged = ArduinoState(
                seq=max(sensor_state.seq, motor_seq),
                gps_lat=sensor_state.gps_lat,
                gps_lon=sensor_state.gps_lon,
                gps_alt=sensor_state.gps_alt,
                gps_spd=sensor_state.gps_spd,
                gps_course=sensor_state.gps_course,
                gps_sats=sensor_state.gps_sats,
                gps_sats_view=sensor_state.gps_sats_view,
                gps_sats_used=sensor_state.gps_sats_used,
                gps_fix=sensor_state.gps_fix,
                heading=sensor_state.heading,
                compass_x=sensor_state.compass_x,
                compass_y=sensor_state.compass_y,
                compass_z=sensor_state.compass_z,
                sensors=sensor_state.sensors,
                mode=mode,
                rc_ch1=rc_ch1,
                rc_ch2=rc_ch2,
                timestamp=max(sensor_state.timestamp, time.time()),
            )

        # Override GPS with the direct Pi NMEA source if one is configured
        if self._gps_reader is not None:
            g = self._gps_reader.get_position()
            merged.gps_lat = g["lat"]
            merged.gps_lon = g["lon"]
            merged.gps_alt = g["alt"]
            merged.gps_spd = g["spd"]
            merged.gps_course = g["course"]
            merged.gps_sats = g["sats"]
            merged.gps_fix = g["fix"]

        # Staleness overrides
        if self._gps_reader is None and self._sensor_gps.is_stale():
            merged.gps_fix = 0  # triggers GpsWatchdog in navigation.py
            log.debug("DualArduinoLink: sensor_gps stale → gps_fix=0")

        if self._gps_reader is not None:
            if self._gps_reader.is_stale():
                merged.gps_fix = 0
                log.debug("DualArduinoLink: pi gps stale → gps_fix=0")
            else:
                merged.gps_fix = max(merged.gps_fix, 0)

        if self._motor_rc is not None and self._motor_rc.is_stale():
            merged.mode = "MANUAL"  # fail-safe: assume least-trusting option
            log.debug("DualArduinoLink: motor_rc stale → mode=MANUAL")

        return merged

    def send_motor_command(self, left: int, right: int) -> None:
        """Send motor command to the Arduino (dual-board via MotorRcLink, single-board via SensorGpsLink)."""
        if self._motor_rc is not None:
            self._motor_rc.send_motor_command(left, right)
        else:
            self._sensor_gps.send_motor_command(left, right)

    def send_ping(self) -> None:
        """Send heartbeat/ping to the Arduino."""
        if self._motor_rc is not None:
            self._motor_rc.send_ping()
        else:
            self._sensor_gps.send_ping()

    def close(self) -> None:
        """Close all links."""
        self._sensor_gps.close()
        if self._motor_rc is not None:
            self._motor_rc.close()
        if self._gps_reader is not None:
            self._gps_reader.close()
        log.info("DualArduinoLink closed")


# ---------------------------------------------------------------------------
# Legacy single Arduino link (backward compatibility)
# ---------------------------------------------------------------------------

class ArduinoLink:
    """
    Single-board UART link (original design).

    Kept for backward compatibility with existing tests and configurations.
    For new dual-board or differential-drive setups, use DualArduinoLink instead.
    """

    def __init__(
        self,
        device: str = "/dev/ttyAMA0",
        baud: int = 115200,
        reconnect_backoff_s: float = 2.0,
        sensor_keys: list[str] | None = None,
    ):
        self._device = device
        self._baud = baud
        self._reconnect_backoff_s = reconnect_backoff_s
        self._sensor_keys = sensor_keys or ["ph", "tds", "turb"]
        self._serial = None
        self._latest = ArduinoState()
        self._lock = threading.Lock()
        self._running = False
        self._reader_thread: threading.Thread | None = None

    def open(self) -> None:
        """Open the serial port and start the background reader."""
        import serial
        self._serial = serial.Serial(self._device, self._baud, timeout=1)
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        log.info(f"ArduinoLink opened on {self._device} @ {self._baud}")

    def _read_loop(self) -> None:
        """Background thread: read lines, parse, update _latest."""
        while self._running:
            try:
                raw = self._serial.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="ignore").strip()
                if not line:
                    continue
                self._parse_line(line)
            except Exception as exc:
                log.warning(f"ArduinoLink serial read error: {exc}")
                time.sleep(self._reconnect_backoff_s)

    def _parse_line(self, line: str) -> None:
        """Parse one JSON line into an ArduinoState and store it."""
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.debug(f"ArduinoLink: malformed JSON: {line!r}")
            return

        try:
            gps = obj.get("gps", {})
            sensors = obj.get("sensors", {})
            rc = obj.get("rc", {})
            compass = obj.get("compass", {})

            # Handle both new format (sats_view/sats_used) and legacy (sats)
            sats_view = int(gps.get("sats_view", 0))
            sats_used = int(gps.get("sats_used", 0))
            sats_legacy = int(gps.get("sats", 0))
            sats_total = sats_used if sats_used else sats_legacy

            state = ArduinoState(
                seq=int(obj.get("seq", 0)),
                gps_lat=float(gps.get("lat", 0.0)),
                gps_lon=float(gps.get("lon", 0.0)),
                gps_alt=float(gps.get("alt", 0.0)),
                gps_spd=float(gps.get("spd", 0.0)),
                gps_course=float(gps.get("course", 0.0)),
                gps_sats=sats_total,
                gps_sats_view=sats_view,
                gps_sats_used=sats_used,
                gps_fix=int(gps.get("fix", 0)),
                heading=float(obj.get("hdg", 0.0)),
                compass_x=int(compass.get("x", 0)),
                compass_y=int(compass.get("y", 0)),
                compass_z=int(compass.get("z", 0)),
                sensors={k: float(sensors.get(k, 0.0)) for k in self._sensor_keys},
                mode=str(obj.get("mode", "MANUAL")),
                rc_ch1=int(rc.get("ch1", 1500)),
                rc_ch2=int(rc.get("ch2", 1500)),
                timestamp=time.time(),
            )

            with self._lock:
                self._latest = state

            log.debug(f"[UART RX] {line!r}")

        except (KeyError, ValueError, TypeError) as exc:
            log.debug(f"ArduinoLink: parse error: {exc} in {line!r}")

    def get_latest_state(self) -> ArduinoState:
        """Return the most recent parsed packet (never blocks)."""
        with self._lock:
            return self._latest

    def send_motor_command(self, left: int, right: int) -> None:
        """Send a motor power command to the Arduino."""
        left = max(-100, min(100, int(left)))
        right = max(-100, min(100, int(right)))
        msg = json.dumps({"cmd": "motor", "l": left, "r": right}) + "\n"
        log.debug(f"[UART TX] {msg.strip()!r}")
        self._write(msg)

    def send_ping(self) -> None:
        """Send a heartbeat/no-op to keep the link alive."""
        msg = json.dumps({"cmd": "ping"}) + "\n"
        log.debug(f"[UART TX] {msg.strip()!r}")
        self._write(msg)

    def _write(self, data: str) -> None:
        """Write raw bytes to serial; log and ignore on error."""
        if self._serial is None or not self._serial.is_open:
            return
        try:
            self._serial.write(data.encode("ascii"))
        except Exception as exc:
            log.warning(f"ArduinoLink write error: {exc}")

    def close(self) -> None:
        """Stop the reader thread and close the serial port."""
        self._running = False
        if self._reader_thread:
            self._reader_thread.join(timeout=3.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        log.info("ArduinoLink closed")


# ---------------------------------------------------------------------------
# Simulated Arduino link (bench testing)
# ---------------------------------------------------------------------------

class SimulatedArduinoLink:
    """
    Simulated Arduino for bench testing without hardware.

    Generates a random-walk GPS track with configurable waypoints,
    synthetic sensor values with noise, and toggles between AUTO/MANUAL
    modes on a timer.  Includes compass XYZ and GPS sats_view/sats_used
    to match the real Arduino packet format.
    """

    def __init__(
        self,
        sensor_keys: list[str] | None = None,
        base_lat: float = 12.91686,
        base_lon: float = 77.48698,
    ):
        self._sensor_keys = sensor_keys or ["ph", "tds", "turb"]
        self._base_lat = base_lat
        self._base_lon = base_lon
        self._latest = ArduinoState()
        self._seq = 0
        self._start_time = time.monotonic()
        self._mode = "AUTO"
        self._lat = base_lat
        self._lon = base_lon
        self._heading = 0.0

    def open(self) -> None:
        """No-op for simulation."""
        self._start_time = time.monotonic()
        log.info("SimulatedArduinoLink opened")

    def get_latest_state(self) -> ArduinoState:
        """Generate and return a synthetic telemetry snapshot."""
        self._seq += 1
        elapsed = time.monotonic() - self._start_time

        # Random-walk GPS
        self._lat += random.uniform(-0.000005, 0.000005)
        self._lon += random.uniform(-0.000005, 0.000005)
        self._heading = (self._heading + random.uniform(-5, 5)) % 360.0

        # Synthetic sensors with realistic ranges + noise
        sensors = {}
        defaults = {"ph": 7.2, "tds": 350.0, "turb": 15.0}
        for k in self._sensor_keys:
            base = defaults.get(k, 0.0)
            sensors[k] = round(base + random.uniform(-0.5, 0.5), 2)

        # Compass: noisy XYZ around the current heading
        import math
        rad = math.radians(self._heading)
        sensors_compass_x = int(-300 * math.sin(rad) + random.uniform(-10, 10))
        sensors_compass_y = int(300 * math.cos(rad) + random.uniform(-10, 10))
        sensors_compass_z = int(50 + random.uniform(-5, 5))

        # Toggle mode periodically for testing
        if int(elapsed) % 120 < 60:
            self._mode = "AUTO"
        else:
            self._mode = "MANUAL"

        state = ArduinoState(
            seq=self._seq,
            gps_lat=self._lat,
            gps_lon=self._lon,
            gps_alt=840.0 + random.uniform(-0.1, 0.1),
            gps_spd=0.4 + random.uniform(-0.1, 0.1),
            gps_course=self._heading,
            gps_sats=8,
            gps_sats_view=10,
            gps_sats_used=8,
            gps_fix=1,
            heading=self._heading,
            compass_x=sensors_compass_x,
            compass_y=sensors_compass_y,
            compass_z=sensors_compass_z,
            sensors=sensors,
            mode=self._mode,
            rc_ch1=1500,
            rc_ch2=1500,
            timestamp=time.time(),
        )
        self._latest = state
        return state

    def send_motor_command(self, left: int, right: int) -> None:
        """Log simulated motor command."""
        log.debug(f"[SIM] motor l={left} r={right}")

    def send_ping(self) -> None:
        """Log simulated ping."""
        log.debug("[SIM] ping")

    def close(self) -> None:
        """No-op for simulation."""
        log.info("SimulatedArduinoLink closed")
