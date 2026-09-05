# Lake Bot — Field Test Checklist

Use this checklist in order. **Do not proceed to a later step until the earlier step passes.**

---

## Step 1 — Bench Dry-Run (Simulated GPS + Mock Motors)

> **Goal**: Confirm the full nav loop, telemetry server, and dashboard work end-to-end before
> touching any physical hardware.

- [ ] SSH into the Pi and navigate to `~/lake-bot/nav_service/`
- [ ] Install dependencies: `pip install -r requirements.txt`
- [ ] Start the service in full simulation mode:
  ```bash
  python3 main.py --config config.yaml --motors mock --simulate-gps
  ```
- [ ] On your laptop, start the dashboard: `./run.sh <pi-ip>`
- [ ] Open `http://localhost:8080` in a browser
- [ ] Confirm the **PI ONLINE** badge appears green within 5 seconds
- [ ] Confirm the bot marker appears on the map and moves (random walk)
- [ ] Click the map to set GCS; confirm `set_gcs` is sent and the GCS stat updates
- [ ] Use **+ Path** to add 3 waypoints; confirm the dashed teal path appears
- [ ] Click **START** — confirm mission state transitions IDLE → RUNNING
- [ ] Watch the bot marker move and the orange breadcrumb trail grow
- [ ] Confirm the mission completes (state → COMPLETE) or click STOP
- [ ] Inspect `nav_log.db` with `sqlite3 nav_log.db "SELECT * FROM events LIMIT 20;"`
  to confirm telemetry and events are being logged

- [ ] Test CLI-only autostart (no dashboard):
  ```bash
  python3 main.py --config config.yaml --motors mock --simulate-gps \
      --waypoints waypoints_sample.json --autostart
  ```
  Confirm the mission runs and completes without a browser connection.

---

## Step 2 — Bench GPIO Test (Motors Disconnected from Driver Board)

> **Goal**: Confirm correct PWM direction signals on the L298N IN/EN pins
> *before* connecting a wheel or prop. Use a multimeter or oscilloscope.

- [ ] Switch to GPIO mode but **do not connect any motors or propellers**:
  ```bash
  python3 main.py --config config.yaml --motors gpio --simulate-gps \
      --waypoints waypoints_sample.json --autostart
  ```
- [ ] Probe BCM GPIO 12 (Left PWM) with a multimeter on AC-millivolts mode.
  At cruise power 18/30 ≈ 60% duty, you should read a frequency signal.
- [ ] Probe BCM GPIO 5 (Left DIR_A) — should be HIGH when driving forward.
- [ ] Probe BCM GPIO 6 (Left DIR_B) — should be LOW when driving forward.
- [ ] Repeat for right motor: GPIO 13 (PWM), GPIO 16 (DIR_A), GPIO 20 (DIR_B).
- [ ] Verify that when STOP is sent, both PWM lines go LOW and both DIR pairs go low.
- [ ] Confirm no smoke, no heat, no unexpected current draw on the L298N board.

---

## Step 3 — GPS Cold-Fix Wait

> **Goal**: Ensure the GPS module has acquired a valid fix before trusting
> any position data outdoors.

- [ ] Power the Pi outdoors (or near a window with sky view) with the GPS antenna attached.
- [ ] Run the service with real GPS (no `--simulate-gps`):
  ```bash
  python3 main.py --config config.yaml --motors mock
  ```
- [ ] Watch the dashboard telemetry panel:
  - **FIX** should move from `0` to `1` or `2` (DGPS) within 30–60 s on a cold start.
  - **SAT** count should reach ≥ 4 before trusting the fix (≥ 6 recommended).
- [ ] Note: A *cold start* (no recent almanac) can take up to **2–5 minutes** in open sky.
  A *warm start* (almanac saved, was used recently) is typically 5–15 s.
- [ ] Do **not** set waypoints or start a mission until SAT ≥ 4 and FIX ≥ 1.
- [ ] Verify the latitude/longitude shown on the dashboard matches the known GPS location
  of your bench (use Google Maps to cross-check).

---

## Step 4 — Compass Calibration (QMC5883L)

> **Goal**: Reduce hard-iron distortion before relying on compass heading.

- [ ] Confirm the compass is detected: in the terminal logs, you should **not** see the
  "Compass unavailable, falling back to GPS course_deg" warning.
- [ ] Perform the *figure-8 swing* calibration procedure:
  - Hold the bot in your hands.
  - Move it slowly in a figure-8 pattern in the horizontal plane for ~30 seconds.
  - This exposes the sensor to the full 360° of the horizontal magnetic field.
- [ ] If the compass always reads the same heading regardless of orientation,
  the I2C address or register configuration may be wrong — re-check `config.yaml`
  (`compass.i2c_address: 0x0D`) and the `CompassDriver.open()` control register write.
- [ ] For critical heading accuracy, compare the compass heading on the dashboard
  with a known compass bearing (e.g. phone compass). They should agree within ±15°.
- [ ] If using GPS-course fallback (compass disabled), the heading is only valid when
  the boat is moving. Expect noisy heading at low speeds or when stationary.

---

## Step 5 — First On-Water Test

> **Goal**: Short 2–3 waypoint loop at minimum safe power on calm water.

### Pre-launch checks
- [ ] Motor power cap in `config.yaml` confirmed at `max_motor_power: 30`
- [ ] `waypoints_sample.json` loaded with a short, simple loop (< 50 m each leg)
- [ ] GCS set to the launch point so the 150 m geofence is centred on you
- [ ] Physical tether (rope) attached to the boat — keep hold of the other end
- [ ] Kill-switch plan: someone standing by the Pi power supply, ready to cut power
- [ ] Phone / tablet running the dashboard visible to the operator at all times

### Launch sequence
- [ ] Place boat in water, motors in water but **not running**
- [ ] Start nav_service on Pi (SSH must stay connected, or use `tmux`/`screen`)
- [ ] Confirm PI ONLINE badge in dashboard
- [ ] Confirm GPS fix quality ≥ 1 and satellite count ≥ 4 on the dashboard
- [ ] Send **START** from dashboard
- [ ] Observe boat motion: should track toward Waypoint 1
- [ ] Monitor orange breadcrumb vs. planned path — they should roughly align
- [ ] Watch for unexpected spinning or reverse motion → press STOP immediately

### Post-test
- [ ] Press STOP and retrieve the boat before the battery is drained
- [ ] Download `nav_log.db` via SCP and review the telemetry table for any anomalies
- [ ] Tune `steering_k` in `config.yaml` if the boat oscillates (decrease) or responds
  sluggishly (increase)
- [ ] Note cold-water runtime and battery voltage for future reference
