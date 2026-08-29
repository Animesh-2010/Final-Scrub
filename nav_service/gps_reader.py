"""
gps_reader.py — Direct GPS reading on the Raspberry Pi.

Parses NMEA-0183 sentences (GGA + RMC) from a GPS module connected
directly to the Pi over serial (USB/UART). Provides lat/lon/alt/spd/
course/sats/fix that can be merged into the boat's ArduinoState,
decoupling boat position from the sensor Arduino.

No external geo/parser dependency is required — only pyserial.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional


log = logging.getLogger(__name__)


class GpsReader:
    """
    Reads NMEA from a serial device in a background thread and exposes the
    latest parsed fix. Thread-safe read via a lock.

    Baud is typically 9600 for a NEO-M8N / many USB GPS modules.
    """

    def __init__(
        self,
        device: str = "/dev/ttyUSB1",
        baud: int = 9600,
        reconnect_backoff_s: float = 2.0,
        staleness_timeout_s: float = 5.0,
    ):
        self._device = device
        self._baud = baud
        self._reconnect_backoff_s = reconnect_backoff_s
        self._staleness_timeout_s = staleness_timeout_s
        self._serial = None
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._gps_lat = 0.0
        self._gps_lon = 0.0
        self._gps_alt = 0.0
        self._gps_spd = 0.0
        self._gps_course = 0.0
        self._gps_sats = 0
        self._gps_fix = 0
        self._last_valid_time = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        import serial
        try:
            self._serial = serial.Serial(self._device, self._baud, timeout=1)
        except Exception as exc:
            log.warning(f"GpsReader: could not open {self._device} — GPS source disabled: {exc}")
            return
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True, name="pi-gps-reader")
        self._reader_thread.start()
        log.info(f"GpsReader opened on {self._device} @ {self._baud}")

    def _read_loop(self) -> None:
        while self._running:
            try:
                raw = self._serial.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="ignore").strip()
                if not line:
                    continue
                self._parse_nmea(line)
            except Exception as exc:
                log.warning(f"GpsReader serial read error: {exc}")
                time.sleep(self._reconnect_backoff_s)

    # ------------------------------------------------------------------
    # NMEA parsing (GGA for fix/lat/lon/alt/sats, RMC for speed/course)
    # ------------------------------------------------------------------

    def _parse_nmea(self, line: str) -> None:
        if not line.startswith("$"):
            return
        fields = line.split(",")
        sentence = fields[0]
        try:
            if sentence == "$GPGGA" or sentence == "$GNGGA":
                self._parse_gga(fields)
            elif sentence == "$GPRMC" or sentence == "$GNRMC":
                self._parse_rmc(fields)
        except (ValueError, IndexError):
            log.debug(f"GpsReader: malformed NMEA: {line!r}")

    def _parse_gga(self, f: list[str]) -> None:
        # $GPGGA,time,lat,N,lon,E,quality,numSV,HDOP,alt,M,...
        if len(f) < 14:
            return
        fix = int(float(f[6])) if f[6] else 0
        lat = self._to_decimal_degrees(f[2], f[3]) if fix > 0 else 0.0
        lon = self._to_decimal_degrees(f[4], f[5]) if fix > 0 else 0.0
        sats = int(float(f[7])) if f[7] else 0
        alt = float(f[9]) if f[9] else 0.0
        with self._lock:
            self._gps_fix = 1 if fix > 0 else 0
            self._gps_lat = lat
            self._gps_lon = lon
            self._gps_sats = sats
            self._gps_alt = alt
            if self._gps_fix > 0:
                self._last_valid_time = time.time()

    def _parse_rmc(self, f: list[str]) -> None:
        # $GPRMC,time,status,lat,N,lon,E,spd,course,date,...
        if len(f) < 10:
            return
        status = f[2]
        if status != "A":  # 'A' = data valid
            return
        lat = self._to_decimal_degrees(f[3], f[4])
        lon = self._to_decimal_degrees(f[5], f[6])
        spd_knots = float(f[7]) if f[7] else 0.0
        course = float(f[8]) if f[8] else 0.0
        with self._lock:
            self._gps_fix = 1
            self._gps_lat = lat
            self._gps_lon = lon
            self._gps_spd = spd_knots * 1.852  # knots -> km/h (matches Arduino gps.spd.kmph())
            self._gps_course = course
            self._last_valid_time = time.time()

    @staticmethod
    def _to_decimal_degrees(raw: str, hemi: str) -> float:
        if not raw:
            return 0.0
        value = float(raw)
        deg = int(value / 100)
        minutes = value - deg * 100
        dd = deg + minutes / 60.0
        if hemi in ("S", "W"):
            dd = -dd
        return dd

    # ------------------------------------------------------------------
    # Read access
    # ------------------------------------------------------------------

    def get_position(self) -> dict:
        with self._lock:
            return {
                "lat": self._gps_lat,
                "lon": self._gps_lon,
                "alt": self._gps_alt,
                "spd": self._gps_spd,
                "course": self._gps_course,
                "sats": self._gps_sats,
                "fix": self._gps_fix,
            }

    def is_stale(self) -> bool:
        if self._last_valid_time == 0.0:
            return True
        return (time.time() - self._last_valid_time) > self._staleness_timeout_s

    def close(self) -> None:
        self._running = False
        if self._reader_thread:
            self._reader_thread.join(timeout=3.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        log.info("GpsReader closed")
