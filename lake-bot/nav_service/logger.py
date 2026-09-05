"""
logger.py — SQLite telemetry and event logger for Lake Bot nav_service.

Tables:
  telemetry : one row per 3 Hz tick
  events    : state transitions, commands, warnings, errors
"""

import sqlite3
import time
import threading
from pathlib import Path


class NavLogger:
    """Thread-safe SQLite logger."""

    def __init__(self, db_path: str = "nav_log.db"):
        self._db_path = str(Path(db_path))
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection = sqlite3.connect(
            self._db_path, check_same_thread=False
        )
        self._create_tables()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS telemetry (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp     REAL    NOT NULL,
                    lat           REAL,
                    lon           REAL,
                    heading_deg   REAL,
                    speed_mps     REAL,
                    state         TEXT,
                    waypoint_index INTEGER,
                    left_power    INTEGER,
                    right_power   INTEGER
                );

                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp  REAL    NOT NULL,
                    event_type TEXT    NOT NULL,
                    detail     TEXT
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def log_telemetry(
        self,
        *,
        lat: float,
        lon: float,
        heading_deg: float,
        speed_mps: float,
        state: str,
        waypoint_index: int,
        left_power: int,
        right_power: int,
        timestamp: float | None = None,
    ) -> None:
        ts = timestamp if timestamp is not None else time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO telemetry
                    (timestamp, lat, lon, heading_deg, speed_mps, state,
                     waypoint_index, left_power, right_power)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, lat, lon, heading_deg, speed_mps, state,
                 waypoint_index, left_power, right_power),
            )
            self._conn.commit()

    def log_event(
        self,
        event_type: str,
        detail: str = "",
        timestamp: float | None = None,
    ) -> None:
        ts = timestamp if timestamp is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (timestamp, event_type, detail) VALUES (?, ?, ?)",
                (ts, event_type, detail),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
