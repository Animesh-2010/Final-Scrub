"""
supabase_sync.py — Supabase cloud relay for SCRUB v4 nav_service.

Responsibilities:
  1. Telemetry push: writes nav telemetry to Supabase `telemetry` table at ~1 Hz.
     Runs in a background thread. The asyncio broadcast loop calls
     enqueue_telemetry() which is non-blocking (thread-safe Queue).

  2. Command poll: background thread polls the `commands` table every 0.5 s for
     unexecuted rows. Adds them to a thread-safe pending list.
     The asyncio task calls drain_commands(nav_ctrl) which pops the list and
     dispatches to NavigationController on the event loop.

Threading model:
  - _push_thread  : consumes _telemetry_queue → Supabase INSERT
  - _poll_thread  : queries commands table → _pending_commands list
  - drain_commands(): called from asyncio, protected by Lock, zero blocking

Usage:
  sync = SupabaseSync(url=..., key=..., enabled=True)
  sync.start()
  # In broadcast loop:
  sync.enqueue_telemetry(telem_dict)
  # In asyncio command drain task:
  sync.drain_commands(nav_ctrl)
  # On shutdown:
  sync.stop()
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# supabase-py is optional — gracefully disabled if not installed
_supabase_available = False
try:
    from supabase import create_client, Client
    _supabase_available = True
except ImportError:
    log.warning("supabase-py not installed — SupabaseSync disabled. Run: pip install supabase")


# ---------------------------------------------------------------------------
# SupabaseSync
# ---------------------------------------------------------------------------

class SupabaseSync:
    """
    Background Supabase sync: telemetry push + command poll.
    All Supabase I/O is confined to background threads.
    """

    def __init__(
        self,
        url: str = "",
        key: str = "",
        enabled: bool = False,
        telemetry_push_hz: float = 1.0,
        command_poll_hz: float = 2.0,
        sensor_batch_interval_s: float = 120.0,
        system_push_hz: float = 1.0,
        telemetry_queue_maxsize: int = 30,
    ):
        self._url = url
        self._key = key
        self._enabled = enabled and _supabase_available and bool(url) and bool(key)
        self._telemetry_interval = 1.0 / max(0.1, telemetry_push_hz)
        self._command_poll_interval = 1.0 / max(0.1, command_poll_hz)
        self._system_interval = 1.0 / max(0.1, system_push_hz)

        # Sensor batch upload (every 2 min by default)
        self._sensor_batch_interval_s = max(1.0, sensor_batch_interval_s)

        self._client: Optional[Any] = None  # supabase.Client
        self._client_lock = threading.RLock()   # guards client rebuild across threads
        self._running = False

        # Telemetry push queue (asyncio → push thread)
        self._telemetry_queue: queue.Queue = queue.Queue(maxsize=telemetry_queue_maxsize)

        # Sensor batch queue (asyncio → push thread, flushed every 2 min)
        self._sensor_queue: queue.Queue = queue.Queue(maxsize=2000)

        # Pi system info queue (asyncio → push thread)
        self._system_queue: queue.Queue = queue.Queue(maxsize=10)

        # Pending commands (poll thread → drain_commands caller)
        self._pending_commands: list[dict] = []
        self._pending_lock = threading.Lock()

        self._push_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._system_thread: Optional[threading.Thread] = None
        self._sensor_thread: Optional[threading.Thread] = None

        self._warned_once = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Initialize the Supabase client and start background threads."""
        if not self._enabled:
            if not self._warned_once:
                log.warning(
                    "SupabaseSync disabled — check: supabase.enabled=true, URL and key set, "
                    "and supabase-py installed."
                )
                self._warned_once = True
            return

        if not _supabase_available:
            log.warning("supabase-py not available — SupabaseSync cannot start.")
            self._enabled = False
            return

        try:
            self._client = create_client(self._url, self._key)
            log.info(f"SupabaseSync connected to {self._url}")
        except Exception as exc:
            log.error(f"SupabaseSync: failed to create Supabase client: {exc}")
            self._enabled = False
            return

        self._running = True

        self._push_thread = threading.Thread(
            target=self._push_loop, daemon=True, name="supabase-push"
        )
        self._push_thread.start()

        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="supabase-poll"
        )
        self._poll_thread.start()

        self._system_thread = threading.Thread(
            target=self._system_loop, daemon=True, name="supabase-system"
        )
        self._system_thread.start()

        self._sensor_thread = threading.Thread(
            target=self._sensor_loop, daemon=True, name="supabase-sensor"
        )
        self._sensor_thread.start()

        log.info("SupabaseSync started (push + poll + system + sensor threads running)")

    def stop(self) -> None:
        """Stop background threads gracefully."""
        self._running = False
        # Unblock the push thread if it's waiting on the queue
        try:
            self._telemetry_queue.put_nowait(None)  # sentinel
        except queue.Full:
            pass
        try:
            self._system_queue.put_nowait(None)  # sentinel
        except queue.Full:
            pass
        try:
            self._sensor_queue.put_nowait(None)  # sentinel
        except queue.Full:
            pass
        if self._push_thread:
            self._push_thread.join(timeout=3.0)
        if self._poll_thread:
            self._poll_thread.join(timeout=3.0)
        if self._system_thread:
            self._system_thread.join(timeout=3.0)
        if self._sensor_thread:
            self._sensor_thread.join(timeout=3.0)

        log.info("SupabaseSync stopped")

    # ------------------------------------------------------------------
    # Thread-safe request helper
    # ------------------------------------------------------------------

    def _execute(self, fn, *, attempts: int = 2):
        """
        Run a supabase query callable. On a connection-level error the shared
        httpx client may be left in a stale "Server disconnected" state, so the
        client is rebuilt once and the call retried. Serialised per-thread by
        the client lock to avoid concurrent-use races on the httpx session.
        """
        import httpx

        def _conn_error(exc) -> bool:
            return isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.TransportError))

        last_exc = None
        for attempt in range(attempts):
            with self._client_lock:
                client = self._client
                try:
                    return fn(client)
                except Exception as exc:
                    last_exc = exc
                    if not _conn_error(exc) or attempt >= attempts - 1:
                        raise
                    # Rebuild the client to recover from a dead connection
                    try:
                        self._client = create_client(self._url, self._key)
                        log.warning("SupabaseSync: connection error — rebuilt client")
                    except Exception as rebuild_exc:
                        log.warning(f"SupabaseSync: failed to rebuild client: {rebuild_exc}")
        raise last_exc

    # ------------------------------------------------------------------
    # Telemetry push (called from asyncio broadcast loop)
    # ------------------------------------------------------------------

    def enqueue_telemetry(self, telem: dict) -> None:
        """
        Non-blocking. Drop oldest if queue is full.
        Called from asyncio — just puts to a thread-safe queue.
        """
        if not self._enabled:
            return
        try:
            self._telemetry_queue.put_nowait(telem)
        except queue.Full:
            try:
                self._telemetry_queue.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            try:
                self._telemetry_queue.put_nowait(telem)
            except queue.Full:
                pass

    def _push_loop(self) -> None:
        """Background thread: drain telemetry queue → Supabase INSERT."""

        while self._running:
            try:
                telem = self._telemetry_queue.get(timeout=self._telemetry_interval)
                if telem is None:  # sentinel
                    break

                row = self._build_telemetry_row(telem)
                self._execute(lambda c: c.table("telemetry").insert(row).execute())

            except queue.Empty:
                pass  # nothing to push — loop again
            except Exception as exc:
                log.warning(f"SupabaseSync push error: {exc}")
                time.sleep(2.0)  # back off on error



    @staticmethod
    def _build_telemetry_row(telem: dict) -> dict:
        """Convert the broadcast telemetry dict to a Supabase row dict."""
        return {
            "lat":                 telem.get("lat"),
            "lon":                 telem.get("lon"),
            "heading_deg":         telem.get("heading_deg"),
            "speed_mps":           telem.get("speed_mps"),
            "state":               telem.get("state"),
            "effective_mode":      telem.get("effective_mode"),
            "mission_id":          telem.get("mission_id"),
            "waypoint_index":      telem.get("current_waypoint_index"),
            "total_waypoints":     telem.get("total_waypoints"),
            "left_power":          telem.get("left_power"),
            "right_power":         telem.get("right_power"),
            "satellites":          telem.get("satellites"),
            "sats_view":           telem.get("sats_view"),
            "sats_used":           telem.get("sats_used"),
            "fix_quality":         telem.get("fix_quality"),
            "compass":             telem.get("compass"),
            "target_bearing":      telem.get("target_bearing"),
            "heading_error":       telem.get("heading_error"),
            "distance_to_target":  telem.get("distance_to_target"),
            "motor_direction":     telem.get("motor_direction"),
            "motor_angle_deg":     telem.get("motor_angle_deg"),
            "motor_rpm":           telem.get("motor_rpm"),
        }

    # ------------------------------------------------------------------
    # Sensor batch upload (every N seconds, default 2 min)
    # ------------------------------------------------------------------

    def enqueue_sensor_reading(self, row: dict) -> None:
        """
        Non-blocking. Queue a sensor reading to be batch-uploaded.
        Called from the asyncio periodic logger (every ~20 s).
        """
        if not self._enabled:
            return
        try:
            self._sensor_queue.put_nowait(row)
        except queue.Full:
            pass

    def _sensor_loop(self) -> None:
        """Background thread: accumulate sensor readings, flush in batches."""

        batch: list[dict] = []
        last_flush = time.monotonic()
        while self._running:
            waited = time.monotonic() - last_flush
            timeout = max(0.1, self._sensor_batch_interval_s - waited)
            try:
                item = self._sensor_queue.get(timeout=timeout)
                if item is None:  # sentinel
                    break
                batch.append(item)
            except queue.Empty:
                pass

            # Flush when interval elapsed (or on sentinel)
            if (time.monotonic() - last_flush) >= self._sensor_batch_interval_s:
                if batch:
                    self._flush_sensor_batch(batch)
                    batch = []
                last_flush = time.monotonic()

        # Flush any remaining on shutdown
        if batch:
            self._flush_sensor_batch(batch)


    def _flush_sensor_batch(self, batch: list[dict]) -> None:
        try:
            self._execute(lambda c: c.table("sensor_readings").insert(batch).execute())

        except Exception as exc:
            log.warning(f"SupabaseSync sensor batch push error: {exc}")

    # ------------------------------------------------------------------
    # Pi system info push (~1 Hz)
    # ------------------------------------------------------------------

    def enqueue_system_info(self, row: dict) -> None:
        """Non-blocking. Queue a Pi system info row for upload at ~1 Hz."""
        if not self._enabled:
            return
        try:
            self._system_queue.put_nowait(row)
        except queue.Full:
            try:
                self._system_queue.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            try:
                self._system_queue.put_nowait(row)
            except queue.Full:
                pass

    def _system_loop(self) -> None:
        """Background thread: push Pi system info rows to Supabase at ~1 Hz."""

        while self._running:
            try:
                row = self._system_queue.get(timeout=self._system_interval)
                if row is None:  # sentinel
                    break
                self._execute(lambda c: c.table("pi_system").insert(row).execute())
            except queue.Empty:
                pass
            except Exception as exc:
                log.warning(f"SupabaseSync system push error: {exc}")
                time.sleep(2.0)


    # ------------------------------------------------------------------
    # Command poll (background thread) + drain (asyncio side)
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """
        Background thread: poll commands table for unexecuted rows.
        Adds them to _pending_commands. Marks each as executed immediately
        so we never double-dispatch.
        """

        while self._running:
            try:
                result = self._execute(
                    lambda c: (
                        c.table("commands")
                        .select("id, type, payload")
                        .eq("executed", False)
                        .order("id")
                        .limit(20)
                        .execute()
                    )
                )
                rows = result.data or []

                if rows:
                    ids = [r["id"] for r in rows]
                    # Mark executed BEFORE dispatching — prevent double-dispatch
                    # even if the process crashes between poll and drain.
                    executed_at = datetime.now(timezone.utc).isoformat()
                    self._execute(
                        lambda c: c.table("commands").update(
                            {"executed": True, "executed_at": executed_at}
                        ).in_("id", ids).execute()
                    )

                    with self._pending_lock:
                        self._pending_commands.extend(rows)

            except Exception as exc:
                log.warning(f"SupabaseSync poll error: {exc}")
                time.sleep(2.0)

            time.sleep(self._command_poll_interval)



    def drain_commands(self, nav_ctrl) -> None:
        """
        Called from the asyncio event loop. Pops all pending commands and
        dispatches them to the NavigationController. Thread-safe hand-off.
        """
        with self._pending_lock:
            if not self._pending_commands:
                return
            cmds = list(self._pending_commands)
            self._pending_commands.clear()

        for cmd in cmds:
            try:
                self._dispatch(cmd, nav_ctrl)
            except Exception as exc:
                log.warning(f"SupabaseSync dispatch error for cmd {cmd}: {exc}")

    def _dispatch(self, cmd: dict, nav) -> None:
        """Route a command dict to the NavigationController."""
        cmd_type = cmd.get("type", "")
        payload  = cmd.get("payload") or {}

        # payload may arrive as a string (JSONB decoded as str in some drivers)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}



        if cmd_type == "load_mission":
            waypoints = payload.get("waypoints", [])
            if waypoints:
                nav.load_mission(waypoints)
            else:
                log.warning("load_mission command has no waypoints — ignored")

        elif cmd_type == "start_mission":
            mission_id = payload.get("mission_id")
            if mission_id is not None:
                # Fetch waypoints + start on a background thread so the blocking
                # Supabase query never stalls the event loop.
                def _do_start():
                    try:
                        wps = self.get_mission_waypoints(int(mission_id))
                        if wps:
                            nav.load_mission(wps)
                            nav.cmd_start()
                            nav.mission_id = int(mission_id)
                            self.update_mission_status(int(mission_id), "RUNNING")
                            log.info(f"start_mission {mission_id}: loaded {len(wps)} waypoints and started")
                        else:
                            log.warning(f"start_mission: no waypoints found for mission {mission_id}")
                    except Exception as exc:
                        log.warning(f"start_mission failed for mission {mission_id}: {exc}")

                threading.Thread(target=_do_start, daemon=True, name="supabase-start-mission").start()
            else:
                log.warning("start_mission command has no mission_id — ignored")

        elif cmd_type == "start":
            nav.cmd_start()

        elif cmd_type == "pause":
            nav.cmd_pause()

        elif cmd_type == "stop":
            nav.cmd_stop()

        elif cmd_type == "emergency_stop":
            nav.cmd_emergency_stop()

        elif cmd_type == "set_speed":
            power = payload.get("power")
            if power is not None:
                nav.set_cruise_power(int(power))

        elif cmd_type == "set_gcs":
            lat = payload.get("lat")
            lon = payload.get("lon")
            if lat is not None and lon is not None:
                nav.gcs_lat = float(lat)
                nav.gcs_lon = float(lon)


        elif cmd_type == "set_manual":
            enabled = payload.get("enabled", False)
            nav.set_dashboard_override(bool(enabled))

        else:
            log.warning(f"SupabaseSync: unknown command type '{cmd_type}' — ignored")

    # ------------------------------------------------------------------
    # Waypoint results (called from nav decision task)
    # ------------------------------------------------------------------

    def push_waypoint_result(
        self,
        mission_id: int,
        waypoint_index: int,
        lat: float,
        lon: float,
        arrived_at: float,
        sample_count: int,
        avg_sensors: dict,
    ) -> None:
        """
        Non-blocking push of a completed waypoint result.
        Queued via a lightweight thread so it doesn't block the nav loop.
        """
        if not self._enabled:
            return

        def _do_push():
            try:
                self._client.table("waypoint_results").insert({
                    "mission_id":      mission_id,
                    "waypoint_index":  waypoint_index,
                    "lat":             lat,
                    "lon":             lon,
                    "arrived_at":      arrived_at,
                    "sample_count":    sample_count,
                    "avg_sensors":     avg_sensors,
                }).execute()
            except Exception as exc:
                log.warning(f"SupabaseSync waypoint_result push error: {exc}")

        threading.Thread(target=_do_push, daemon=True, name="supabase-wp-push").start()

    def push_mission_created(self, mission_id: int, waypoint_count: int) -> None:
        """Sync a new mission record to Supabase."""
        if not self._enabled:
            return

        def _do_push():
            try:
                # Upsert by id so we don't double-insert if the Pi reconnects
                self._client.table("missions").upsert({
                    "id":             mission_id,
                    "waypoint_count": waypoint_count,
                    "status":         "RUNNING",
                }).execute()
            except Exception as exc:
                log.warning(f"SupabaseSync mission create push error: {exc}")

        threading.Thread(target=_do_push, daemon=True, name="supabase-mission-push").start()

    def push_mission_status(self, mission_id: int, status: str) -> None:
        """Update mission status in Supabase (e.g., COMPLETE, STOPPED)."""
        if not self._enabled:
            return

        def _do_push():
            try:
                self._client.table("missions").update({"status": status}).eq("id", mission_id).execute()
            except Exception as exc:
                log.warning(f"SupabaseSync mission status push error: {exc}")

        threading.Thread(target=_do_push, daemon=True, name="supabase-status-push").start()

    # ------------------------------------------------------------------
    # Mission fetch (Pi pulls its planned GPS points from Supabase)
    # ------------------------------------------------------------------

    def get_mission_waypoints(self, mission_id: int) -> list[dict]:
        """
        Fetch the ordered waypoints for a mission from Supabase.
        Returns a list of {"lat": ..., "lon": ...} as expected by
        NavigationController.load_mission(). Throws on error.
        """
        if not self._enabled:
            raise RuntimeError("SupabaseSync disabled — cannot fetch mission waypoints")

        result = self._execute(
            lambda c: (
                c.table("mission_waypoints")
                .select("lat, lon")
                .eq("mission_id", mission_id)
                .order("seq")
                .execute()
            )
        )
        rows = result.data or []
        return [{"lat": float(r["lat"]), "lon": float(r["lon"])} for r in rows]

    def get_mission(self, mission_id: int) -> Optional[dict]:
        """Fetch a single mission record, or None if it doesn't exist."""
        if not self._enabled:
            return None
        try:
            result = self._execute(
                lambda c: (
                    c.table("missions")
                    .select("*")
                    .eq("id", mission_id)
                    .limit(1)
                    .execute()
                )
            )
            rows = result.data or []
            return rows[0] if rows else None
        except Exception as exc:
            log.warning(f"SupabaseSync get_mission error: {exc}")
            return None

    def update_mission_status(self, mission_id: int, status: str) -> None:
        """Synchronously update a mission's status (blocking, called from dispatch)."""
        if not self._enabled:
            return
        try:
            self._client.table("missions").update({"status": status}).eq("id", mission_id).execute()
        except Exception as exc:
            log.warning(f"SupabaseSync update_mission_status error: {exc}")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled
