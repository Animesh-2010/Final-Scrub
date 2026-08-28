# Lake Bot — Autonomous GPS Navigation System

> Raspberry Pi 5 paddle-wheel boat with u-blox NEO-M8N GPS + QMC5883L compass,
> L298N dual H-bridge motors, WebSocket telemetry, and a live Leaflet.js mission planner.

---

## Repository Structure

```
lake-bot/
├── nav_service/          # Runs on the Raspberry Pi over SSH
├── dashboard/            # Runs on your laptop
├── tests/                # Full unit + integration test suite
├── docs/                 # Field test checklist
├── conftest.py           # pytest hardware stubs (no hardware needed for tests)
├── pytest.ini
├── requirements-dev.txt
└── README.md
```

---

## 1. Running nav_service on the Raspberry Pi

### Prerequisites (Pi side)
```bash
# On the Pi
cd ~/lake-bot/nav_service
pip install -r requirements.txt
```

### Dashboard-driven mode (wait for commands from dashboard)
```bash
# Simulation (mock GPS + mock motors) — bench testing
python3 main.py --config config.yaml --motors mock --simulate-gps

# Real hardware (GPIO motors + real GPS)
python3 main.py --config config.yaml --motors gpio
```

### CLI autostart mode (no dashboard needed — bench/CI)
```bash
# Load waypoints from file and immediately start the mission
python3 main.py --config config.yaml --motors mock --simulate-gps \
    --waypoints waypoints_sample.json --autostart

# With a custom GPS track file for simulation
python3 main.py --config config.yaml --motors mock \
    --simulate-gps --simulate-gps-track my_track.json \
    --waypoints waypoints_sample.json --autostart
```

> **Tip**: Run `nav_service` inside a `tmux` or `screen` session so SSH
> disconnects don't kill the process:
> ```bash
> tmux new-session -s lakebot
> python3 main.py --config config.yaml --motors gpio
> # Detach: Ctrl-B, D
> ```

---

## 2. Running the Dashboard (Laptop)

### Prerequisites (laptop side)
- Python 3.11+ and `bash` (Git Bash on Windows, or WSL)
- The Pi must be reachable on your LAN

### Start the dashboard
```bash
cd lake-bot/dashboard
./run.sh 192.168.1.42        # Replace with your Pi's IP address
```

The script:
1. Creates a Python virtual environment in `dashboard/.venv` if not present
2. Installs `requirements.txt`
3. Starts the FastAPI server on `http://localhost:8080`

Open **http://localhost:8080** in your browser. The **PI ONLINE** badge turns
green when the WebSocket connection to the Pi is established.

---

## 3. Switching from Mock to GPIO Motors

### Step 1 — Verify wiring
Confirm your L298N wiring matches the pin table in `config.yaml`:

| Motor | Signal | BCM GPIO |
|-------|--------|----------|
| Left  | PWM    | 12       |
| Left  | DIR_A  | 5        |
| Left  | DIR_B  | 6        |
| Right | PWM    | 13       |
| Right | DIR_A  | 16       |
| Right | DIR_B  | 20       |

### Step 2 — Change the CLI flag
```bash
# Before (mock):
python3 main.py --config config.yaml --motors mock --simulate-gps

# After (real GPIO):
python3 main.py --config config.yaml --motors gpio
```

### Step 3 — Custom pin numbers
If your wiring differs from the table above, edit `config.yaml`:
```yaml
motors:
  left_pwm_pin:   12    # Change to your actual BCM pin
  left_dir_a_pin: 5
  left_dir_b_pin: 6
  right_pwm_pin:  13
  right_dir_a_pin: 16
  right_dir_b_pin: 20
  pwm_frequency: 1000
```
No code changes needed — `main.py` reads all pins from the config file.

---

## 4. Running the Test Suite

```bash
# From the lake-bot/ root on any machine (no hardware required)
pip install -r requirements-dev.txt
pip install -r nav_service/requirements.txt   # pure-Python deps (pynmea2, etc.)
pytest tests/
```

All hardware (GPIO, I2C, UART serial) is stubbed in `conftest.py`. The suite
runs fully offline with no physical hardware attached.

### Test coverage summary

| Test file | What it covers |
|---|---|
| `test_gps_driver.py` | NMEA parsing, interpolation, random walk |
| `test_compass_driver.py` | I2C heading math, error propagation |
| `test_navigation.py` | Haversine, bearing, steering, state machine |
| `test_motor_driver.py` | MockMotorDriver logging, power clamping |
| `test_safety.py` | Geofence, GPS watchdog, mission timer |
| `test_telemetry_schema.py` | JSON schema, unknown commands, load_mission |
| `test_integration_simulated_mission.py` | Full mission, geofence breach, GPS loss |

---

## 5. Waypoints Format

The waypoints JSON file is a simple array of objects with `lat` and `lon` fields:

```json
[
  {"lat": 12.91700, "lon": 77.48710},
  {"lat": 12.91720, "lon": 77.48720},
  {"lat": 12.91740, "lon": 77.48705}
]
```

- Waypoints are visited in order, index 0 first.
- The mission is complete when all waypoints have been reached (within the
  `waypoint_arrival_radius_m: 2.5` threshold).
- You can also set waypoints interactively from the dashboard by using the
  **+ Path** button and clicking on the map.

---

## 6. Configuration Reference (`config.yaml`)

All parameters that can be tuned without touching code:

```yaml
navigation:
  max_motor_power: 30          # Safety cap (0–100 scale). Never increase past 100.
  default_cruise_power: 18     # Starting power level
  waypoint_arrival_radius_m: 2.5
  steering_k: 0.6              # Proportional gain. Increase for faster turns.

safety:
  geofence_radius_m: 150
  gps_loss_watchdog_timeout_s: 5
  max_mission_runtime_s: 1800

telemetry:
  websocket_port: 8765
  broadcast_rate_hz: 3
```

---

## 7. Hardware Reference

- **Compute**: Raspberry Pi 5 (Raspberry Pi OS 64-bit, Bookworm)
- **GPS**: u-blox NEO-M8N via UART `/dev/ttyAMA0` at 9600 baud
- **Compass**: QMC5883L at I2C bus 1, address `0x0D`
- **Motor driver**: L298N dual H-bridge
- **GPIO library**: `gpiozero` + `lgpio` (required for Pi 5 RP1 chip — **not** RPi.GPIO)
- **Dashboard WebSocket port**: 8765 (Pi) → relayed on 8080 (laptop)

---

## 8. Safety Notes

- The motor power hard cap is 30 (on a -100..100 scale). This is enforced in
  `navigation.py::compute_wheel_powers()` regardless of what the dashboard slider shows.
- The geofence will stop the mission if the boat travels > 150 m from the GCS position.
- GPS signal loss for > 5 seconds while running triggers an automatic stop.
- SIGINT (`Ctrl-C`) or SIGTERM calls `motors.stop()` before exiting.
- See `docs/field_test_checklist.md` for the complete pre-flight and on-water procedure.
