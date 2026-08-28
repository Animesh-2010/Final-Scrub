"""
firebase_sync.py — Optional Firebase Realtime Database integration for SCRUB v4.

Provides:
  - Live mirror: overwrites a single RTDB node with latest telemetry at ~1 Hz.
  - Waypoint summaries: writes averaged sensor results per waypoint.

Must be entirely optional and non-blocking: if firebase.enabled is false
or credentials are missing, log a warning once and continue with local
SQLite only. Uses a decoupled bounded queue + background task pattern.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

# Firebase is optional — only import if available
_firebase_available = False
try:
    import firebase_admin
    from firebase_admin import credentials, db

    _firebase_available = True
except ImportError:
    pass


class FirebaseSync:
    """
    Background Firebase Realtime Database sync via a bounded asyncio queue
    (drop-oldest on backpressure). Never blocks the nav/motor loops.
    """

    def __init__(
        self,
        enabled: bool = True,
        credentials_path: str = "firebase-service-account.json",
        database_url: str = "",
        live_path: str = "live_telemetry",
        missions_path: str = "missions",
        live_push_hz: float = 1.0,
        queue_max_size: int = 50,
    ):
        self._enabled = enabled and _firebase_available
        self._credentials_path = credentials_path
        self._database_url = database_url
        self._live_path = live_path
        self._missions_path = missions_path
        self._live_push_interval = 1.0 / live_push_hz if live_push_hz > 0 else 1.0
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max_size)
        self._initialized = False
        self._warned_once = False

    def initialize(self) -> None:
        """
        Attempt to initialize Firebase. Call once at startup.
        Non-fatal: logs warning and continues if unavailable.
        """
        if not self._enabled:
            if not self._warned_once:
                log.warning("Firebase sync disabled (firebase.enabled=false or SDK missing)")
                self._warned_once = True
            return

        if self._initialized:
            return

        if not os.path.exists(self._credentials_path):
            log.warning(f"Firebase credentials not found: {self._credentials_path} — continuing without Firebase")
            self._enabled = False
            self._warned_once = True
            return

        if not self._database_url or "YOUR_PROJECT_ID" in self._database_url:
            log.warning("firebase.database_url is missing or still a placeholder — continuing without Firebase")
            self._enabled = False
            self._warned_once = True
            return

        try:
            cred = credentials.Certificate(self._credentials_path)
            firebase_admin.initialize_app(
                cred,
                {"databaseURL": self._database_url},
            )
            self._initialized = True
            log.info("Firebase Realtime Database initialized")
        except Exception as exc:
            log.warning(f"Firebase init failed: {exc} — continuing without Firebase")
            self._enabled = False
            self._warned_once = True

    # ------------------------------------------------------------------
    # Live telemetry mirror
    # ------------------------------------------------------------------

    def push_live_telemetry(self, telemetry: dict) -> None:
        """
        Enqueue a live telemetry update (non-blocking, drop-oldest on backpressure).
        """
        if not self._enabled or not self._initialized:
            return
        try:
            self._queue.put_nowait(("live", telemetry))
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # drop oldest
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(("live", telemetry))
            except asyncio.QueueEmpty:
                pass

    # ------------------------------------------------------------------
    # Waypoint summary
    # ------------------------------------------------------------------

    def push_waypoint_summary(
        self, mission_id: int, waypoint_index: int, data: dict
    ) -> None:
        """Enqueue a waypoint summary write."""
        if not self._enabled or not self._initialized:
            return
        payload = {"mission_id": mission_id, "waypoint_index": waypoint_index, **data}
        try:
            self._queue.put_nowait(("waypoint", payload))
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(("waypoint", payload))
            except asyncio.QueueEmpty:
                pass

    # ------------------------------------------------------------------
    # Background consumer task
    # ------------------------------------------------------------------

    async def run_consumer(self) -> None:
        """
        Background task: consume the queue and perform RTDB writes.
        Runs forever; cancellation handled by the caller.
        """
        if not self._enabled or not self._initialized:
            return

        while True:
            try:
                msg_type, payload = await self._queue.get()

                if msg_type == "live":
                    # Overwrite single RTDB node for live telemetry
                    db.reference(self._live_path).set(payload)

                elif msg_type == "waypoint":
                    mission_id = payload.pop("mission_id")
                    waypoint_index = payload.pop("waypoint_index")
                    path = f"{self._missions_path}/{mission_id}/waypoints/{waypoint_index}"
                    db.reference(path).set(payload)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning(f"Firebase RTDB write error: {exc}")
                await asyncio.sleep(0.1)
