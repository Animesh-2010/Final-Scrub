"""
system_monitor.py — Raspberry Pi processing-info sampler for SCRUB v4.

Collects lightweight system metrics (CPU %, RAM %, thermal temp, uptime)
plus navigation internals so the dashboard can show "Raspberry Pi
processing information".

Uses psutil if available (graceful fallback to /proc + sysfs so the module
still runs on minimal installs).
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional


def _read_sysf_file(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


class SystemMonitor:
    """Periodic sampler of Pi CPU/RAM/temp + navigation internals."""

    def __init__(self) -> None:
        self._cpu_last: Optional[tuple[int, int]] = None  # (idle, total) ticks
        self._uptime_start = time.time()
        try:
            import psutil  # type: ignore
            self._psutil = psutil
        except ImportError:
            self._psutil = None

    # ------------------------------------------------------------------
    # Public sampling API (cheap, no blocking I/O in hot paths)
    # ------------------------------------------------------------------

    def cpu_percent(self) -> float:
        if self._psutil is not None:
            try:
                return round(self._psutil.cpu_percent(interval=None), 1)
            except Exception:
                pass
        # Manual /proc/stat delta
        line = _read_sysf_file("/proc/stat")
        if not line or not line.startswith("cpu "):
            return 0.0
        parts = line.split()[1:]
        if len(parts) < 4:
            return 0.0
        try:
            idle = int(parts[3])
            total = sum(int(p) for p in parts)
        except (ValueError, IndexError):
            return 0.0

        if self._cpu_last is not None:
            idle_delta = idle - self._cpu_last[0]
            total_delta = total - self._cpu_last[1]
            if total_delta > 0:
                pct = 100.0 * (1.0 - idle_delta / total_delta)
                self._cpu_last = (idle, total)
                return round(max(0.0, min(100.0, pct)), 1)
        self._cpu_last = (idle, total)
        return 0.0

    def memory_percent(self) -> float:
        if self._psutil is not None:
            try:
                return round(self._psutil.virtual_memory().percent, 1)
            except Exception:
                pass
        # Fallback: /proc/meminfo
        try:
            with open("/proc/meminfo") as f:
                data = {}
                for line in f:
                    k, rest = line.split(":", 1)
                    data[k] = int(rest.strip().split()[0])  # kB
            total = data.get("MemTotal", 0)
            avail = data.get("MemAvailable", 0)
            if total:
                return round(100.0 * (total - avail) / total, 1)
        except (OSError, ValueError, IndexError):
            pass
        return 0.0

    def cpu_temp_c(self) -> float:
        # Raspberry Pi thermal zone
        for i in range(0, 5):
            val = _read_sysf_file(f"/sys/class/thermal/thermal_zone{i}/temp")
            if val is not None:
                try:
                    return round(float(val) / 1000.0, 1)
                except ValueError:
                    continue
        if self._psutil is not None:
            try:
                temps = self._psutil.sensors_temperatures()
                for entries in temps.values():
                    if entries:
                        return round(entries[0].current, 1)
            except Exception:
                pass
        return 0.0

    def uptime_s(self) -> float:
        return round(time.time() - self._uptime_start, 1)

    # ------------------------------------------------------------------
    # Build a full row
    # ------------------------------------------------------------------

    def build_system_row(self, nav, mission_id: Optional[int]) -> dict:
        last_error = getattr(nav, "last_error", None)
        return {
            "mission_id":     mission_id,
            "cpu_pct":        self.cpu_percent(),
            "ram_pct":        self.memory_percent(),
            "cpu_temp_c":     self.cpu_temp_c(),
            "uptime_s":       self.uptime_s(),
            "state":          getattr(nav, "state", None).value if getattr(nav, "state", None) else None,
            "effective_mode": getattr(nav, "_effective_mode", lambda: None)(),
            "left_power":     getattr(nav, "left_power", None),
            "right_power":    getattr(nav, "right_power", None),
            "motor_direction": getattr(nav, "motor_direction", None),
            "motor_angle_deg": getattr(nav, "motor_angle_deg", None),
            "motor_rpm":       getattr(nav, "motor_rpm", None),
            "nav_tick_ms":    getattr(nav, "last_nav_tick_ms", None),
            "last_error":     last_error,
        }
