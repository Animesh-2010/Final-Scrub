-- ============================================================
-- SCRUB v4 — Supabase Schema
-- Run this in your Supabase project → SQL Editor
-- ============================================================
--
-- Data model (v2):
--   missions          : the durable mission PLAN created by the dashboard
--   mission_waypoints : the GPS points belonging to a mission (the Pi FETCHES these)
--   telemetry         : live position/state stream (~1 Hz, Realtime for the map)
--   sensor_readings   : batch-uploaded sensor data (every 2 min from the Pi)
--   pi_system         : Raspberry Pi processing info (CPU/RAM/temp/nav internals)
--   commands          : dashboard -> Pi command relay
--   waypoint_results  : averaged sensor readings per completed dwell stop


-- ─── MISSIONS (mission plan, created by the dashboard) ────────
-- The dashboard creates one row per planned mission. The Pi references
-- this id, fetches its waypoints, and updates status as it runs.

create table if not exists missions (
  id             bigserial primary key,
  created_at     timestamptz default now(),
  name           text,
  status         text not null default 'PLANNED',
  -- status values: 'PLANNED', 'RUNNING', 'PAUSED', 'COMPLETE', 'STOPPED', 'ABORTED'
  waypoint_count int not null default 0,
  gcs_lat        float8,
  gcs_lon        float8
);

create index if not exists missions_status_idx on missions (status);


-- ─── MISSION WAYPOINTS (the planned GPS points) ───────────────
-- One row per GPS point. Inserted by the dashboard in batch, then
-- fetched by the Raspberry Pi when the mission starts.

create table if not exists mission_waypoints (
  id         bigserial primary key,
  mission_id bigint not null references missions(id) on delete cascade,
  seq        int not null,
  lat        float8 not null,
  lon        float8 not null
);

create index if not exists mission_waypoints_mission_idx on mission_waypoints (mission_id, seq);


-- ─── TELEMETRY (live position/state, ~1 Hz) ──────────────────
-- One row per broadcast tick (~1 Hz from Pi).
-- Dashboard subscribes via Realtime to get live position/state.
-- Kept lightweight (no sensor blob) for a smooth live map.

create table if not exists telemetry (
  id                bigserial primary key,
  inserted_at       timestamptz default now(),
  mission_id        bigint references missions(id),
  lat               float8,
  lon               float8,
  heading_deg       float8,
  speed_mps         float8,
  state             text,
  effective_mode    text,
  waypoint_index    int,
  total_waypoints   int,
  left_power        int,
  right_power       int,
  satellites        int,
  fix_quality       int,
  target_bearing    float8,
  heading_error     float8,
  distance_to_target float8,
  motor_direction   text,
  motor_angle_deg   float8,
  motor_rpm         float8,
  motor_pwm_l       int,
  motor_pwm_r       int
);

create index if not exists telemetry_inserted_at_idx on telemetry (inserted_at desc);


-- ─── SENSOR READINGS (batch upload ~every 2 min) ─────────────
-- The Pi logs sensors locally every 20 s and batch-uploads here every
-- 2 minutes. Used for trend charts / water-quality mapping.

create table if not exists sensor_readings (
  id          bigserial primary key,
  inserted_at timestamptz default now(),
  mission_id  bigint references missions(id),
  lat         float8,
  lon         float8,
  sensors     jsonb       -- {"ph": 7.2, "tds": 412, "turb": 18, ...}
);

create index if not exists sensor_readings_mission_idx on sensor_readings (mission_id, inserted_at desc);


-- ─── PI SYSTEM (Raspberry Pi processing info) ────────────────
-- Folded-in system + navigation internals published by the Pi at ~1 Hz
-- so the dashboard can show "Raspberry Pi processing information".

create table if not exists pi_system (
  id             bigserial primary key,
  inserted_at    timestamptz default now(),
  mission_id     bigint references missions(id),
  cpu_pct        float8,
  ram_pct        float8,
  cpu_temp_c     float8,
  uptime_s       float8,
  state          text,
  effective_mode text,
  left_power     int,
  right_power    int,
  motor_direction text,
  motor_angle_deg float8,
  motor_rpm       float8,
  nav_tick_ms    float8,
  last_error     text
);

create index if not exists pi_system_inserted_at_idx on pi_system (inserted_at desc);


-- ─── COMMANDS (dashboard -> Pi relay) ────────────────────────
-- Dashboard writes rows here. Pi polls at ~2 Hz, executes and
-- marks executed=true. Acts as the cloud command relay.

create table if not exists commands (
  id          bigserial primary key,
  inserted_at timestamptz default now(),
  type        text not null,
  -- Supported types:
  --   'start_mission'  payload: {"mission_id": 5}   -> Pi fetches mission_waypoints for id 5
  --   'pause'          payload: {}
  --   'stop'           payload: {}
  --   'emergency_stop' payload: {}
  --   'set_speed'      payload: {"power": 22}
  --   'set_gcs'        payload: {"lat": 12.91686, "lon": 77.48698}
  --   'set_manual'     payload: {"enabled": true}
  payload     jsonb default '{}',
  executed    boolean default false,
  executed_at timestamptz
);

create index if not exists commands_unexecuted_idx on commands (id) where executed = false;


-- ─── WAYPOINT RESULTS (dwell stop averages) ──────────────────
-- One row per waypoint after dwell completes.
-- Averaged sensor readings for that sampling stop.

create table if not exists waypoint_results (
  id             bigserial primary key,
  inserted_at    timestamptz default now(),
  mission_id     bigint references missions(id),
  waypoint_index int not null,
  lat            float8,
  lon            float8,
  arrived_at     float8,     -- unix timestamp
  sample_count   int,
  avg_sensors    jsonb       -- {"ph": 7.2, "tds": 412, "turb": 18, ...}
);

create index if not exists waypoint_results_mission_idx on waypoint_results (mission_id, waypoint_index);


-- ─── ROW LEVEL SECURITY ──────────────────────────────────────
-- Enable RLS on all tables, then allow all operations.
-- Tighten with auth later if needed.

alter table missions          enable row level security;
alter table mission_waypoints enable row level security;
alter table telemetry         enable row level security;
alter table sensor_readings   enable row level security;
alter table pi_system         enable row level security;
alter table commands          enable row level security;
alter table waypoint_results  enable row level security;

-- Allow all (anon key)
create policy "allow all" on missions          for all using (true) with check (true);
create policy "allow all" on mission_waypoints for all using (true) with check (true);
create policy "allow all" on telemetry         for all using (true) with check (true);
create policy "allow all" on sensor_readings   for all using (true) with check (true);
create policy "allow all" on pi_system         for all using (true) with check (true);
create policy "allow all" on commands          for all using (true) with check (true);
create policy "allow all" on waypoint_results  for all using (true) with check (true);


-- ─── REALTIME ────────────────────────────────────────────────
-- Enable Realtime publication for live dashboard updates.
-- Run these AFTER creating tables.

alter publication supabase_realtime add table telemetry;
alter publication supabase_realtime add table pi_system;
alter publication supabase_realtime add table missions;


-- ─── OPTIONAL: CLEANUP FUNCTIONS ─────────────────────────────
-- Delete old rows to prevent unbounded growth.
-- Schedule via Supabase Dashboard → Database → Scheduled Jobs (pg_cron).

-- create or replace function cleanup_telemetry()
-- returns void language sql as $$
--   delete from telemetry where inserted_at < now() - interval '7 days';
-- $$;
--
-- create or replace function cleanup_sensor_readings()
-- returns void language sql as $$
--   delete from sensor_readings where inserted_at < now() - interval '90 days';
-- $$;
--
-- create or replace function cleanup_pi_system()
-- returns void language sql as $$
--   delete from pi_system where inserted_at < now() - interval '7 days';
-- $$;
