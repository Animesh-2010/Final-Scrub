# SCRUB v4 — Backend (nav_service)

Raspberry Pi 5 backend for the SCRUB lake water quality monitoring boat.

## Quick Start

### Simulate mode (bench test, no hardware)

```bash
cd scrub-v4
pip install -r requirements.txt
python -m nav_service.main --config nav_service/config.yaml --simulate
```

With preloaded waypoints and autostart:

```bash
python -m nav_service.main --config nav_service/config.yaml --simulate \
    --waypoints nav_service/waypoints_sample.json --autostart
```

### Real hardware

```bash
python -m nav_service.main --config nav_service/config.yaml
```

## Architecture

Four independent asyncio tasks (never one big loop):

| Task | Cadence | Latency Budget | Purpose |
|------|---------|----------------|---------|
| `nav_decision_task` | 4 s | < 50 ms | Pull latest Arduino state, compute heading/power |
| `motor_heartbeat_task` | 0.2 s (5 Hz) | < 5 ms | Re-send current power values to Arduino |
| `ws_broadcast_task` | 0.33 s (3 Hz) | < 30 ms | Build telemetry, log to SQLite, broadcast to WS clients |
| `firebase_consumer` | event-driven | < 100 ms | Push telemetry/waypoints to Firebase RTDB |

Serial reads run in two independent background threads (sensor+GPS board on GPIO UART, motor+RC board on USB). SQLite writes are decoupled via the broadcast loop. Firebase pushes use a bounded queue with drop-oldest backpressure.

## UART Wire Protocol (Arduino ⇄ Pi)

Newline-delimited JSON, one object per line, both directions.

### Arduino → Pi

```json
{
  "seq": 1234,
  "gps": {"lat": 12.91686, "lon": 77.48698, "alt": 840.0, "spd": 0.4, "course": 187.2, "sats": 8, "fix": 1},
  "hdg": 187.5,
  "sensors": {"ph": 7.34, "tds": 412.5, "turb": 18.2, "wtemp": 26.1, "atemp": 31.4, "hum": 68.2},
  "mode": "AUTO",
  "rc": {"ch1": 1500, "ch2": 1500}
}
```

- `fix`: 0 = no fix, 1 = GPS fix, 2 = DGPS (NMEA GGA)
- `mode`: `"AUTO"` or `"MANUAL"` (physical RC switch)
- `sensors` keys are config-driven — adding a sensor is a one-line config change

### Pi → Arduino

```json
{"cmd": "motor", "l": 46, "r": 56}
{"cmd": "ping"}
```

- `l`/`r`: signed PWM in `-255..255` (positive = forward, |value| = `analogWrite` duty)
- `ping`: heartbeat/no-op sent when in MANUAL or DWELL

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | `{"status": "ok", "arduino_link": "connected", "uptime_s": ...}` |
| `GET` | `/api/missions` | List past missions |
| `GET` | `/api/missions/{id}/waypoints` | Averaged sensor results per waypoint |
| `GET` | `/api/missions/{id}/dwell_samples?waypoint_index=N` | Raw dwell samples |

## WebSocket API

Connect to `ws://<pi_ip>:8000/ws`.

### Telemetry broadcast (server → client, ~3 Hz)

```json
{
  "type": "telemetry",
  "lat": 12.91686, "lon": 77.48698,
  "heading_deg": 187.5, "speed_mps": 0.4,
  "state": "RUNNING", "effective_mode": "AUTO",
  "current_waypoint_index": 2, "total_waypoints": 5,
  "satellites": 8, "fix_quality": 1,
  "left_power": 18, "right_power": 22,
  "sensors": {"ph": 7.34, "tds": 412.5, "turb": 18.2, "wtemp": 26.1, "atemp": 31.4, "hum": 68.2},
  "timestamp": 1724061000.0
}
```

### Commands (client → server)

```json
{"type": "load_mission", "waypoints": [{"lat": 12.917, "lon": 77.487}, ...]}
{"type": "set_gcs", "lat": 12.91686, "lon": 77.48698}
{"type": "start"}
{"type": "pause"}
{"type": "stop"}
{"type": "set_speed", "power": 22}
{"type": "emergency_stop"}
{"type": "set_manual", "enabled": true}
{"type": "get_history", "mission_id": 1}
```

### Error responses

```json
{"type": "error", "message": "Unknown command type: 'foo'"}
```

## Config File (`config.yaml`)

```yaml
sensor_gps_link:
  device: "/dev/ttyAMA0"
  baud: 115200
  reconnect_backoff_s: 2.0
  staleness_timeout_s: 3.0
  sensor_keys: ["ph", "tds", "turb", "wtemp", "atemp", "hum"]

motor_rc_link:
  device: "/dev/ttyACM0"
  baud: 115200
  reconnect_backoff_s: 2.0
  staleness_timeout_s: 1.0

navigation:
  max_motor_power: 30
  default_cruise_power: 18
  waypoint_arrival_radius_m: 2.0
  steering_k: 0.6
  nav_tick_interval_s: 4.0
  motor_heartbeat_hz: 5.0

dwell:
  duration_s: 30
  sample_interval_s: 2

telemetry:
  websocket_broadcast_hz: 3.0

safety:
  geofence_radius_m: 150
  gps_loss_watchdog_timeout_s: 5
  max_mission_runtime_s: 1800

logger:
  db_path: "nav_log.db"

firebase:
  enabled: true
  credentials_path: "firebase-service-account.json"
  database_url: "https://scrub-v4-default-rtdb.asia-southeast1.firebasedatabase.app/"
  live_path: "live_telemetry"
  missions_path: "missions"
  live_push_hz: 1.0

gcs_lat: 12.91686
gcs_lon: 77.48698
```

## State Machine

```
IDLE → RUNNING → DWELL → RUNNING → ... → COMPLETE
         ↓                    ↓
       PAUSED              MANUAL
         ↓                    ↓
       RUNNING              RUNNING (resume)
         ↓
       STOPPED

Any state → STOPPED (emergency_stop)
RUNNING → GPS_LOST (watchdog)
RUNNING → GEOFENCE_STOP (geofence breach)
```

## Safety

- **Hardware fail-safe**: RC receiver's physical switch is wired directly to Arduino. Pi never drives motors in MANUAL — only pauses its own logic.
- **Effective mode**: `MANUAL if (physical_rc_mode == "MANUAL" or dashboard_override_enabled) else "AUTO"`
- **Geofence**: configurable radius around GCS
- **GPS watchdog**: configurable timeout
- **Mission timer**: configurable max runtime

## Database (SQLite, WAL mode)

- `telemetry` — one row per broadcast tick
- `events` — state transitions, commands, errors
- `missions` — mission metadata
- `dwell_samples` — raw sensor readings during 30 s dwell windows
- `waypoint_results` — averaged sensor values per waypoint

Sensor averages are stored as JSON blobs (`avg_sensors_json`) so adding new sensors never requires a schema migration.

## Running Tests

```bash
cd scrub-v4
pip install -r requirements.txt
pytest tests/ -v
```

## Project Structure

```
scrub-v4/
├── nav_service/
│   ├── main.py            # CLI entrypoint, task wiring
│   ├── config.yaml        # Configuration
│   ├── arduino_link.py    # UART link (real + simulated)
│   ├── navigation.py      # State machine + steering math
│   ├── safety.py          # Geofence, watchdog, timer
│   ├── logger.py          # SQLite logger
│   ├── firebase_sync.py   # Optional Firebase RTDB mirror
│   ├── server.py          # FastAPI app (WS + REST)
│   ├── requirements.txt   # Python dependencies
│   └── waypoints_sample.json
├── tests/
│   ├── test_navigation.py
│   ├── test_safety.py
│   ├── test_arduino_link.py
│   ├── test_telemetry_schema.py
│   ├── test_integration_simulated_mission.py
│   └── test_logger.py
├── conftest.py
├── requirements.txt
└── BACKEND_README.md
```
