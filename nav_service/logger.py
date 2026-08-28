"""
logger.py — SQLite telemetry and event logger for SCRUB v4 nav_service.

Tables:
  telemetry        : one row per broadcast tick
  events           : state transitions, commands, warnings, errors
  missions         : mission metadata
  dwell_samples    : raw sensor readings during dwell windows
  waypoint_results : averaged sensor values per waypoint

Uses WAL mode for concurrent read/write. Thread-safe via threading.Lock.
"""

import json
import sqlite3
import time
import threading
from pathlib import Path


class NavLogger:
    """Thread-safe SQLite logger with decoupled write queue."""

    def __init__(self, db_path: str = "nav_log.db"):
        self._db_path = str(Path(db_path))
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection = sqlite3.connect(
            self._db_path, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
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

                CREATE TABLE IF NOT EXISTS missions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at      REAL    NOT NULL,
                    waypoint_count  INTEGER NOT NULL,
                    status          TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dwell_samples (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id      INTEGER NOT NULL,
                    waypoint_index  INTEGER NOT NULL,
                    timestamp       REAL    NOT NULL,
                    sensors_json    TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS waypoint_results (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id        INTEGER NOT NULL,
                    waypoint_index    INTEGER NOT NULL,
                    lat               REAL,
                    lon               REAL,
                    arrived_at        REAL,
                    sample_count      INTEGER,
                    avg_sensors_json  TEXT
                );

                CREATE TABLE IF NOT EXISTS sensor_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       REAL    NOT NULL,
                    mission_id      INTEGER,
                    lat             REAL,
                    lon             REAL,
                    state           TEXT,
                    sensors_json    TEXT    NOT NULL
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Telemetry
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

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Missions
    # ------------------------------------------------------------------

    def create_mission(self, waypoint_count: int) -> int:
        """Create a new mission record and return its ID."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO missions (created_at, waypoint_count, status) VALUES (?, ?, ?)",
                (time.time(), waypoint_count, "RUNNING"),
            )
            self._conn.commit()
            return cur.lastrowid

    def update_mission_status(self, mission_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE missions SET status = ? WHERE id = ?",
                (status, mission_id),
            )
            self._conn.commit()

    def get_missions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, waypoint_count, status FROM missions ORDER BY id DESC"
            ).fetchall()
        return [
            {"id": r[0], "created_at": r[1], "waypoint_count": r[2], "status": r[3]}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Dwell samples
    # ------------------------------------------------------------------

    def log_dwell_sample(
        self,
        mission_id: int,
        waypoint_index: int,
        sensors: dict[str, float],
        timestamp: float | None = None,
    ) -> None:
        ts = timestamp if timestamp is not None else time.time()
        sensors_json = json.dumps(sensors)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO dwell_samples
                    (mission_id, waypoint_index, timestamp, sensors_json)
                VALUES (?, ?, ?, ?)
                """,
                (mission_id, waypoint_index, ts, sensors_json),
            )
            self._conn.commit()

    def get_dwell_samples(
        self, mission_id: int, waypoint_index: int | None = None
    ) -> list[dict]:
        with self._lock:
            if waypoint_index is not None:
                rows = self._conn.execute(
                    "SELECT mission_id, waypoint_index, timestamp, sensors_json "
                    "FROM dwell_samples WHERE mission_id = ? AND waypoint_index = ? "
                    "ORDER BY timestamp",
                    (mission_id, waypoint_index),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT mission_id, waypoint_index, timestamp, sensors_json "
                    "FROM dwell_samples WHERE mission_id = ? ORDER BY waypoint_index, timestamp",
                    (mission_id,),
                ).fetchall()
        return [
            {
                "mission_id": r[0],
                "waypoint_index": r[1],
                "timestamp": r[2],
                "sensors": json.loads(r[3]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Waypoint results
    # ------------------------------------------------------------------

    def log_waypoint_result(
        self,
        mission_id: int,
        waypoint_index: int,
        lat: float,
        lon: float,
        arrived_at: float,
        sample_count: int,
        avg_sensors: dict[str, float],
    ) -> None:
        avg_json = json.dumps(avg_sensors)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO waypoint_results
                    (mission_id, waypoint_index, lat, lon, arrived_at,
                     sample_count, avg_sensors_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (mission_id, waypoint_index, lat, lon, arrived_at,
                 sample_count, avg_json),
            )
            self._conn.commit()

    def get_waypoint_results(self, mission_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mission_id, waypoint_index, lat, lon, arrived_at, "
                "sample_count, avg_sensors_json "
                "FROM waypoint_results WHERE mission_id = ? ORDER BY waypoint_index",
                (mission_id,),
            ).fetchall()
        return [
            {
                "mission_id": r[0],
                "waypoint_index": r[1],
                "lat": r[2],
                "lon": r[3],
                "arrived_at": r[4],
                "sample_count": r[5],
                "avg_sensors": json.loads(r[6]) if r[6] else {},
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Periodic sensor log (every 20 s)
    # ------------------------------------------------------------------

    def log_sensor(
        self,
        *,
        sensors: dict[str, float],
        mission_id: int | None = None,
        lat: float | None = None,
        lon: float | None = None,
        state: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        ts = timestamp if timestamp is not None else time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sensor_log
                    (timestamp, mission_id, lat, lon, state, sensors_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ts, mission_id, lat, lon, state, json.dumps(sensors)),
            )
            self._conn.commit()

    def get_sensor_log(
        self, mission_id: int | None = None, limit: int = 1000
    ) -> list[dict]:
        with self._lock:
            if mission_id is not None:
                rows = self._conn.execute(
                    "SELECT timestamp, mission_id, lat, lon, state, sensors_json "
                    "FROM sensor_log WHERE mission_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (mission_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT timestamp, mission_id, lat, lon, state, sensors_json "
                    "FROM sensor_log ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "timestamp": r[0],
                "mission_id": r[1],
                "lat": r[2],
                "lon": r[3],
                "state": r[4],
                "sensors": json.loads(r[5]) if r[5] else {},
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()
