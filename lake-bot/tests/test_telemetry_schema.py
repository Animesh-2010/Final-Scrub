"""
tests/test_telemetry_schema.py — Telemetry message shape and command handling tests.

Validates the exact JSON schema from the spec and that malformed/unknown
commands return the documented error shape without crashing.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from navigation import NavState, NavigationController
from motor_driver import MockMotorDriver
from gps_driver import FixResult, SimulatedGpsDriver
from compass_driver import CompassDriver
from logger import NavLogger
from telemetry_server import TelemetryServer


# ─── Helpers ──────────────────────────────────────────────────────────

REQUIRED_TELEMETRY_FIELDS = {
    "type":                    str,
    "lat":                     float,
    "lon":                     float,
    "heading_deg":             float,
    "speed_mps":               float,
    "state":                   str,
    "current_waypoint_index":  int,
    "satellites":              int,
    "fix_quality":             int,
    "left_power":              int,
    "right_power":             int,
    "timestamp":               float,
}


def _make_server() -> tuple[TelemetryServer, NavigationController]:
    fix = FixResult(
        lat=12.91686, lon=77.48698,
        altitude_m=5.0, speed_mps=0.5, course_deg=45.0,
        satellites=8, fix_quality=1, timestamp=time.time(),
    )
    gps = MagicMock(spec=SimulatedGpsDriver)
    gps.get_fix.return_value = fix

    compass = MagicMock(spec=CompassDriver)
    compass.get_heading.return_value = 45.0

    motors = MockMotorDriver()
    logger = MagicMock(spec=NavLogger)

    nav = NavigationController(
        gps=gps, compass=compass, motors=motors, logger=logger,
        arrival_radius_m=2.5, geofence_radius_m=150.0,
        gps_loss_timeout_s=5.0, max_mission_runtime_s=1800.0,
        max_motor_power=30, default_cruise_power=18, steering_k=0.6,
    )

    server = TelemetryServer(nav_ctrl=nav, logger=logger)
    return server, nav


# ─── Telemetry schema ─────────────────────────────────────────────────

def test_telemetry_message_has_all_required_fields():
    """
    _build_telemetry() must produce a dict with every field from the spec,
    each with the correct Python type.
    """
    server, nav = _make_server()
    # Give nav a valid fix via a tick
    nav.tick()

    telem = server._build_telemetry()

    for field, expected_type in REQUIRED_TELEMETRY_FIELDS.items():
        assert field in telem, f"Missing field: '{field}'"
        assert isinstance(telem[field], expected_type), (
            f"Field '{field}': expected {expected_type.__name__}, "
            f"got {type(telem[field]).__name__} = {telem[field]!r}"
        )

    assert telem["type"] == "telemetry"


# ─── Unknown command → error response ────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_command_type_returns_error_not_crash():
    """
    Sending {"type": "not_a_real_command"} must return the error shape
    and must not raise.
    """
    server, nav = _make_server()

    sent_messages = []

    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock(side_effect=lambda msg: sent_messages.append(msg))
    fake_ws.remote_address = ("127.0.0.1", 9999)

    await server._dispatch_command(fake_ws, {"type": "not_a_real_command"})

    # Must have sent exactly one error message
    assert len(sent_messages) == 1
    response = json.loads(sent_messages[0])
    assert response["type"] == "error"
    assert "message" in response
    assert isinstance(response["message"], str)


@pytest.mark.asyncio
async def test_malformed_json_handled_in_dispatch():
    """Empty-type command returns error without crashing."""
    server, nav = _make_server()

    sent_messages = []
    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock(side_effect=lambda msg: sent_messages.append(msg))

    await server._dispatch_command(fake_ws, {})  # missing "type"

    assert len(sent_messages) == 1
    response = json.loads(sent_messages[0])
    assert response["type"] == "error"


# ─── load_mission command ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_mission_command_updates_waypoint_list():
    """
    Sending load_mission must update nav.waypoints to match the payload exactly.
    """
    server, nav = _make_server()

    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock()

    test_waypoints = [
        {"lat": 12.9169, "lon": 77.4870},
        {"lat": 12.9171, "lon": 77.4869},
    ]

    await server._dispatch_command(
        fake_ws,
        {"type": "load_mission", "waypoints": test_waypoints},
    )

    assert len(nav.waypoints) == 2
    assert abs(nav.waypoints[0].lat - 12.9169) < 1e-6
    assert abs(nav.waypoints[0].lon - 77.4870) < 1e-6
    assert abs(nav.waypoints[1].lat - 12.9171) < 1e-6
    assert abs(nav.waypoints[1].lon - 77.4869) < 1e-6
