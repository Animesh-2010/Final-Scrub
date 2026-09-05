# SCRUB v4 — Hosted Dashboard

A static single-page web dashboard for the SCRUB v4 autonomous water-quality boat.
Connects to the Raspberry Pi via **Supabase** as the cloud relay.
Deploy to Vercel, Netlify, or any static host — no server required.

---

## Architecture

```
[This Dashboard]  ←→  [Supabase Cloud]  ←→  [Pi nav_service]
  (anywhere)            (cloud relay)          (local on Pi)
       ↕                     ↕
  Realtime sub         commands table
  (telemetry)          + telemetry table
```

- **Telemetry** (Pi → Dashboard): Pi writes a lightweight position/state row to `telemetry` at ~1 Hz. Dashboard subscribes via Supabase Realtime.
- **Sensors** (Pi → Dashboard): Pi logs sensor readings locally every 20 s and batch-uploads them to `sensor_readings` every 2 min.
- **Pi processing info** (Pi → Dashboard): Pi pushes CPU/RAM/temp/uptime + nav internals to `pi_system` at ~1 Hz.
- **Mission plan** (Dashboard → Supabase): Dashboard inserts a `missions` row + its `mission_waypoints`, then sends `start_mission`.
- **Commands** (Dashboard → Pi): Dashboard inserts into `commands` table. Pi polls at 2 Hz, executes, marks `executed=true`.

---

## Setup (5 steps)

### Step 1 — Create a Supabase project
Go to [supabase.com](https://supabase.com) → New Project → choose a region close to your Pi's location.

### Step 2 — Run the SQL schema
In your Supabase project: **SQL Editor** → paste the contents of `supabase_schema.sql` → Run.

### Step 3 — Get your keys
In your Supabase project: **Settings → API**:
- **Project URL** → copy (e.g. `https://abcdefgh.supabase.co`)
- **anon / public** key → copy (for the dashboard)
- **service_role** key → copy (for the Pi — keep secret!)

### Step 4 — Configure the dashboard
Edit `app.js` lines 16–17:
```js
const SUPABASE_URL      = 'https://YOUR_PROJECT_ID.supabase.co';
const SUPABASE_ANON_KEY = 'your-anon-key-here';
```

### Step 5 — Configure the Pi
Edit `nav_service/config.yaml`:
```yaml
supabase:
  enabled: true
  url: "https://YOUR_PROJECT_ID.supabase.co"
  key: "YOUR_SERVICE_ROLE_KEY"   # ← service_role key, NOT anon
  telemetry_push_hz: 1.0
  command_poll_hz: 2.0
```
Then on the Pi:
```bash
pip install supabase
python3 main.py --config config.yaml
```

---

## Deploy the Dashboard

### Option A — Netlify (easiest, 30 seconds)
1. Go to [netlify.com](https://netlify.com) → Sign up / Log in
2. Drag the `scrub-dashboard/` folder onto the Netlify deploy zone
3. Done — you get a URL like `https://amazing-name-123.netlify.app`

### Option B — Vercel
```bash
npm install -g vercel
cd scrub-dashboard/
vercel --prod
```
Or connect your GitHub repo at [vercel.com](https://vercel.com).

### Option C — GitHub Pages
1. Push `scrub-dashboard/` to a GitHub repo
2. Settings → Pages → Source: Deploy from branch → root
3. Done — `https://username.github.io/repo-name`

---

## Using the Dashboard

| Action | How |
|--------|-----|
| Set GCS | Click "Set GCS", then click on the map |
| Add waypoints | Click "+ Add Path", click map points, click button again to finish |
| Start mission | Click **START** (saves a `missions` plan + `mission_waypoints`, then sends `start_mission`; the Pi fetches the GPS points itself) |
| Emergency stop | Click ⚡ **EMERGENCY STOP** |
| View history | Click "↻ Refresh History", then click a mission to see dwell results on map |
| View sensor history | Click "📈 Sensor History" to load batch-uploaded `sensor_readings` |
| Pi status | "Raspberry Pi Processing" panel shows CPU/RAM/temp/uptime/nav-tick/last-error |

### Map features
- 🟢 **Rotating arrow** = bot's live position + heading
- 🟠 **Orange trail** = breadcrumb path the bot has taken
- 🔵 **Dashed teal line** = planned waypoint path
- 🟠 **Orange circle** = 150m geofence around GCS
- 🟣 **Pulsing purple dots** = completed dwell/sampling stops

### State meanings

| State | Meaning |
|-------|---------|
| `IDLE` | Waiting for START command |
| `RUNNING` | Navigating to next waypoint |
| `DWELL` | Sampling water quality at waypoint |
| `PAUSED` | Mission paused |
| `COMPLETE` | All waypoints visited ✅ |
| `MANUAL` | Physical RC switch in MANUAL mode |
| `GPS_LOST` | No GPS fix for >5s — stopped |
| `GEOFENCE_STOP` | Left the 150m geofence — stopped |

---

## Files

```
scrub-dashboard/
├── index.html   — SPA layout
├── app.js       — Supabase + Leaflet logic
├── style.css    — Dark theme
└── README.md    — This file
```

---

## Pi Requirements
```
# nav_service/requirements.txt (already updated):
supabase>=2.0.0
```

The Pi must have internet access (WiFi or mobile hotspot) to reach Supabase.
The Pi's `nav_service` core logic is unchanged — Supabase is purely additive.
