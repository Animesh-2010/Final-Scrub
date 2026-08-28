import time
import math
from dataclasses import dataclass, field
from typing import Optional

import serial
import pynmea2


@dataclass
class FixResult:
    lat: float = 0.0
    lon: float = 0.0
    altitude_m: float = 0.0
    speed_mps: float = 0.0
    course_deg: float = 0.0
    satellites: int = 0
    fix_quality: int = 0
    timestamp: float = 0.0


class GpsDriver:
    def __init__(self, device: str = "/dev/ttyAMA0", baud: int = 9600):
        self._device = device
        self._baud = baud
        self._serial = None
        self._fix = FixResult()

    def open(self) -> None:
        self._serial = serial.Serial(self._device, self._baud, timeout=1)

    def _parse_sentence(self, raw: str):
        if not raw:
            return None
        try:
            return pynmea2.parse(raw)
        except (pynmea2.ParseError, pynmea2.ChecksumError):
            return None

    def _process_sentence(self, msg) -> None:
        if isinstance(msg, pynmea2.types.talker.GGA):
            self._fix.fix_quality = int(msg.gps_qual) if msg.gps_qual else 0
            self._fix.lat = msg.latitude if msg.latitude else 0.0
            self._fix.lon = msg.longitude if msg.longitude else 0.0
            self._fix.altitude_m = msg.altitude if msg.altitude else 0.0
            self._fix.satellites = int(msg.num_sats) if msg.num_sats else 0
        elif isinstance(msg, pynmea2.types.talker.RMC):
            if msg.spd_over_grnd is not None:
                self._fix.speed_mps = float(msg.spd_over_grnd) * 0.514444
            if msg.true_course is not None:
                self._fix.course_deg = float(msg.true_course)
        elif isinstance(msg, pynmea2.types.talker.VTG):
            if msg.true_track is not None:
                self._fix.course_deg = float(msg.true_track)
            if msg.spd_over_grnd_kmph is not None:
                self._fix.speed_mps = float(msg.spd_over_grnd_kmph) / 3.6

    def get_fix(self) -> FixResult:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            line = self._serial.readline().decode("ascii", errors="ignore").strip()
            if not line:
                continue
            msg = self._parse_sentence(line)
            if msg is not None:
                self._process_sentence(msg)
        self._fix.timestamp = time.time()
        return self._fix

    def close(self):
        if self._serial and self._serial.is_open:
            self._serial.close()


class SimulatedGpsDriver(GpsDriver):
    def __init__(self, track_file: Optional[str] = None, rate_hz: int = 3):
        self._rate_hz = rate_hz
        self._track = []
        self._start_time = time.time()
        self._fix = FixResult()

        if track_file:
            import json
            with open(track_file, "r") as f:
                self._track = json.load(f)
        else:
            self._track = self._default_random_walk()

    def _default_random_walk(self):
        import random
        base_lat, base_lon = 12.91686, 77.48698
        points = [{"lat": base_lat, "lon": base_lon, "timestamp_offset": 0.0}]
        lat, lon = base_lat, base_lon
        for i in range(1, 600):
            lat += random.uniform(-0.00001, 0.00001)
            lon += random.uniform(-0.00001, 0.00001)
            points.append({"lat": lat, "lon": lon, "timestamp_offset": i / self._rate_hz})
        return points

    def open(self) -> None:
        self._start_time = time.time()

    def _interpolate(self, t_offset: float):
        if len(self._track) == 0:
            return 12.91686, 77.48698, 0.0, 0.0
        if len(self._track) == 1:
            p = self._track[0]
            return p["lat"], p["lon"], 0.0, 0.0

        if t_offset <= self._track[0]["timestamp_offset"]:
            p = self._track[0]
            return p["lat"], p["lon"], 0.0, 0.0
        if t_offset >= self._track[-1]["timestamp_offset"]:
            p = self._track[-1]
            return p["lat"], p["lon"], 0.0, 0.0

        for i in range(len(self._track) - 1):
            a = self._track[i]
            b = self._track[i + 1]
            if a["timestamp_offset"] <= t_offset <= b["timestamp_offset"]:
                dt = b["timestamp_offset"] - a["timestamp_offset"]
                if dt == 0:
                    frac = 0.0
                else:
                    frac = (t_offset - a["timestamp_offset"]) / dt
                lat = a["lat"] + frac * (b["lat"] - a["lat"])
                lon = a["lon"] + frac * (b["lon"] - a["lon"])
                dist = self._haversine(a["lat"], a["lon"], lat, lon)
                speed = dist * self._rate_hz if self._rate_hz > 0 else 0.0
                course = self._bearing_deg(a["lat"], a["lon"], lat, lon)
                return lat, lon, speed, course

        p = self._track[-1]
        return p["lat"], p["lon"], 0.0, 0.0

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2):
        R = 6371000.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @staticmethod
    def _bearing_deg(lat1, lon1, lat2, lon2):
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dlam = math.radians(lon2 - lon1)
        x = math.sin(dlam) * math.cos(phi2)
        y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
        brng = math.degrees(math.atan2(x, y))
        return (brng + 360) % 360

    def get_fix(self) -> FixResult:
        t_offset = time.time() - self._start_time
        lat, lon, speed, course = self._interpolate(t_offset)
        self._fix = FixResult(
            lat=lat,
            lon=lon,
            altitude_m=10.0,
            speed_mps=speed,
            course_deg=course,
            satellites=8,
            fix_quality=1,
            timestamp=time.time(),
        )
        return self._fix

    def close(self):
        pass
