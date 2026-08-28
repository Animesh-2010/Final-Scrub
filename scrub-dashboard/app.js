/**
 * app.js — SCRUB v4 Hosted Dashboard
 *
 * Architecture:
 *   - Supabase Realtime subscription → live telemetry from Pi
 *   - Supabase table INSERT → commands to Pi
 *   - Leaflet map: bot marker, breadcrumb, planned path, waypoints, GCS, geofence
 *   - Pi online detection: telemetry freshness check (>10s = offline)
 *
 * No WebSocket server needed. Dashboard is fully static — deploy anywhere.
 *
 * SETUP: Replace the two constants below with your Supabase project values.
 */

'use strict';

// ─── ⚙️  SUPABASE CONFIG — fill these in ─────────────────────────────────────
const SUPABASE_URL = 'https://imjivjqfxwwirlmdsetl.supabase.co';
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imltaml2anFmeHd3aXJsbWRzZXRsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODc5Mjg4ODcsImV4cCI6MjEwMzUwNDg4N30.R1b6BgtFFpvXj2mOl5hoY3bZaxenDXyWKghzx-5z66M';
// ─────────────────────────────────────────────────────────────────────────────

const MAP_CENTER  = [12.91686, 77.48698];
const MAP_ZOOM    = 17;
const GEOFENCE_R  = 150;     // metres — must match nav_service config
const OFFLINE_AFTER_MS = 10000;  // declare Pi offline if no telemetry for 10s

const TILE_LAYERS = {
  street: {
    url:  'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    attr: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZ: 19,
  },
  satellite: {
    url:  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attr: 'Tiles © Esri — Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye',
    maxZ: 18,
  },
};

// ─── Supabase client ─────────────────────────────────────────────────────────
const { createClient } = supabase;
let sb = null;

function initSupabase() {
  if (!SUPABASE_URL || SUPABASE_URL.includes('YOUR_PROJECT_ID')) {
    toast('⚠️ Set SUPABASE_URL and SUPABASE_ANON_KEY in app.js', 'warn', 8000);
    return false;
  }
  sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
  return true;
}

// ─── State ───────────────────────────────────────────────────────────────────
let currentLayer   = 'street';
let leafletLayer   = null;

// GCS
let gcsLatLon  = null;
let gcsMarker  = null;
let gcsMode    = false;
let geoCircle  = null;

// Mission planning
let waypoints    = [];   // flat list of {lat, lon}
let paths        = [];   // [{id, waypoints:[...]}]
let pathMode     = false;
let activePathId = null;
let wpMarkers    = [];
let planPolyline = null;
let missionLoaded = false;

// Bot
let botMarker    = null;
let breadcrumb   = [];
let breadPolyline = null;

// Dwell markers
let dwellMarkers = [];

// Telemetry freshness
let lastTelemetryAt = 0;
let piOnline        = false;
let freshnessTimer  = null;

// ─── Toast ───────────────────────────────────────────────────────────────────
function toast(msg, type = 'info', duration = 3000) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 400);
  }, duration);
}

// ─── Pi online / offline detection ───────────────────────────────────────────
function checkFreshness() {
  const stale = (Date.now() - lastTelemetryAt) > OFFLINE_AFTER_MS;
  if (stale && piOnline) {
    piOnline = false;
    updatePiBadge(false);
  } else if (!stale && !piOnline) {
    piOnline = true;
    updatePiBadge(true);
  }
}

function updatePiBadge(online) {
  const badge = document.getElementById('pi-badge');
  const text  = document.getElementById('pi-badge-text');
  badge.className = `badge badge-${online ? 'online' : 'offline'}`;
  text.textContent = online ? 'PI ONLINE' : 'PI OFFLINE';
}

// ─── Map init ────────────────────────────────────────────────────────────────
const map = L.map('map', { center: MAP_CENTER, zoom: MAP_ZOOM, zoomControl: true });

function initLayer(name) {
  const cfg = TILE_LAYERS[name];
  if (leafletLayer) map.removeLayer(leafletLayer);
  leafletLayer = L.tileLayer(cfg.url, { attribution: cfg.attr, maxZoom: cfg.maxZ });
  leafletLayer.addTo(map);
}
initLayer('street');

// Bot marker (rotating SVG arrow)
function makeBotIcon(deg) {
  return L.divIcon({
    className: '',
    html: `<div style="width:38px;height:38px;transform:rotate(${deg}deg);transform-origin:center;display:flex;align-items:center;justify-content:center;">
      <svg width="38" height="38" viewBox="0 0 38 38" fill="none">
        <circle cx="19" cy="19" r="17" fill="#0d1117" stroke="#2dd4a7" stroke-width="2"/>
        <path d="M19 6 L25 30 L19 25 L13 30 Z" fill="#2dd4a7"/>
      </svg>
    </div>`,
    iconSize:   [38, 38],
    iconAnchor: [19, 19],
  });
}

botMarker = L.marker(MAP_CENTER, { icon: makeBotIcon(0), zIndexOffset: 1000 })
  .addTo(map)
  .bindTooltip('SCRUB', { permanent: false, direction: 'top' });

// Polylines
breadPolyline = L.polyline([], { color: '#f97316', weight: 2, opacity: 0.85 }).addTo(map);
planPolyline  = L.polyline([], { color: '#2dd4a7', weight: 2, opacity: 0.7, dashArray: '6 6' }).addTo(map);

// ─── Marker helpers ───────────────────────────────────────────────────────────
function placeGcsMarker(lat, lon) {
  if (gcsMarker) map.removeLayer(gcsMarker);
  if (geoCircle)  map.removeLayer(geoCircle);

  gcsMarker = L.marker([lat, lon], {
    icon: L.divIcon({ className:'', html:'<div class="gcs-marker">GCS</div>', iconSize:[32,32], iconAnchor:[16,16] }),
  }).addTo(map).bindTooltip(`GCS: ${lat.toFixed(5)}, ${lon.toFixed(5)}`, { direction:'top' });

  geoCircle = L.circle([lat, lon], {
    radius:      GEOFENCE_R,
    color:       '#f97316',
    weight:      1,
    opacity:     0.5,
    fillOpacity: 0.05,
    dashArray:   '4 4',
  }).addTo(map).bindTooltip(`Geofence: ${GEOFENCE_R}m`, { sticky: true });
}

function rebuildWpMarkers() {
  wpMarkers.forEach(m => map.removeLayer(m));
  wpMarkers = [];
  waypoints.forEach((wp, i) => {
    const m = L.marker([wp.lat, wp.lon], {
      icon: L.divIcon({ className:'', html:`<div class="wp-marker">${i+1}</div>`, iconSize:[22,22], iconAnchor:[11,11] }),
    }).addTo(map).bindTooltip(`WP ${i+1}: ${wp.lat.toFixed(5)}, ${wp.lon.toFixed(5)}`, { direction:'top' });
    wpMarkers.push(m);
  });
  planPolyline.setLatLngs(waypoints.map(wp => [wp.lat, wp.lon]));
}

function addDwellMarker(lat, lon, wpIdx) {
  const m = L.marker([lat, lon], {
    icon: L.divIcon({ className:'', html:'<div class="dwell-marker"></div>', iconSize:[18,18], iconAnchor:[9,9] }),
  }).addTo(map).bindPopup(`<b>WP ${wpIdx + 1} — DWELL COMPLETE</b><br>${lat.toFixed(5)}, ${lon.toFixed(5)}`);
  dwellMarkers.push(m);
}

// ─── Path / waypoint management ───────────────────────────────────────────────
function renderPathList() {
  const ul = document.getElementById('path-list');
  ul.innerHTML = '';
  paths.forEach((path, idx) => {
    const li = document.createElement('li');
    li.className = 'path-item';
    li.innerHTML = `
      <span class="path-item-name">Path ${idx+1} <span style="color:var(--text-muted);font-size:9px">(${path.waypoints.length} pts)</span></span>
      <div class="path-controls">
        <button class="path-btn" onclick="movePath(${idx},-1)">▲</button>
        <button class="path-btn" onclick="movePath(${idx},1)">▼</button>
        <button class="path-btn del" onclick="deletePath(${idx})">×</button>
      </div>`;
    ul.appendChild(li);
  });
  waypoints = paths.flatMap(p => p.waypoints);
  missionLoaded = false;
  rebuildWpMarkers();
  updateStats();
}

function movePath(idx, dir) {
  const ni = idx + dir;
  if (ni < 0 || ni >= paths.length) return;
  [paths[idx], paths[ni]] = [paths[ni], paths[idx]];
  renderPathList();
}

function deletePath(idx) {
  const path = paths[idx];
  if (activePathId === path.id) {
    pathMode = false; activePathId = null;
    document.getElementById('btn-add-path').classList.remove('active');
    document.body.classList.remove('mode-path');
  }
  paths.splice(idx, 1);
  renderPathList();
}

// ─── Map click handler ────────────────────────────────────────────────────────
map.on('click', (e) => {
  const { lat, lng: lon } = e.latlng;
  if (gcsMode) {
    gcsLatLon = [lat, lon];
    placeGcsMarker(lat, lon);
    document.getElementById('gcs-lat-val').textContent = lat.toFixed(6);
    document.getElementById('gcs-lon-val').textContent = lon.toFixed(6);
    document.getElementById('gcs-info').classList.remove('hidden');
    updateStats();
    sendCommand('set_gcs', { lat, lon });
    toast(`GCS set: ${lat.toFixed(5)}, ${lon.toFixed(5)}`, 'success');
    gcsMode = false;
    document.getElementById('btn-set-gcs').classList.remove('active');
    document.body.classList.remove('mode-gcs');
  } else if (pathMode && activePathId !== null) {
    const path = paths.find(p => p.id === activePathId);
    if (path) {
      path.waypoints.push({ lat, lon });
      renderPathList();
    }
  }
});

// ─── UI helpers ───────────────────────────────────────────────────────────────
function haversineM(la1, lo1, la2, lo2) {
  const R = 6371000, d2r = Math.PI/180;
  const dlat = (la2-la1)*d2r, dlon = (lo2-lo1)*d2r;
  const a = Math.sin(dlat/2)**2 + Math.cos(la1*d2r)*Math.cos(la2*d2r)*Math.sin(dlon/2)**2;
  return 2*R*Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}

function totalMissionDistance() {
  if (waypoints.length < 2) return 0;
  let d = 0;
  for (let i = 0; i < waypoints.length - 1; i++)
    d += haversineM(waypoints[i].lat, waypoints[i].lon, waypoints[i+1].lat, waypoints[i+1].lon);
  return d;
}

function updateStats() {
  document.getElementById('stat-waypoints').textContent = waypoints.length;
  const dist = totalMissionDistance();
  document.getElementById('stat-distance').textContent = dist > 1000
    ? `${(dist/1000).toFixed(2)} km` : `${Math.round(dist)} m`;
  document.getElementById('stat-gcs').textContent = gcsLatLon ? 'SET' : 'UNSET';
}

const STATE_DOT_MAP = {
  IDLE:          'dot-idle',
  RUNNING:       'dot-running',
  PAUSED:        'dot-paused',
  COMPLETE:      'dot-complete',
  STOPPED:       'dot-stopped',
  DWELL:         'dot-dwell',
  GEOFENCE_STOP: 'dot-geofence',
  GPS_LOST:      'dot-gpslost',
  MANUAL:        'dot-manual',
};

function updateStatusUI(state) {
  const dot   = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  dot.className = `dot ${STATE_DOT_MAP[state] || 'dot-idle'}`;
  label.textContent = state || 'IDLE';
}

// ─── Telemetry handler ────────────────────────────────────────────────────────
let _prevState = null;

function onTelemetry(row) {
  lastTelemetryAt = Date.now();
  checkFreshness();

  const lat = row.lat || 0;
  const lon = row.lon || 0;

  // Bot marker
  botMarker.setLatLng([lat, lon]);
  botMarker.setIcon(makeBotIcon(row.heading_deg || 0));

  // Breadcrumb trail (keep last 500 points)
  breadcrumb.push([lat, lon]);
  if (breadcrumb.length > 500) breadcrumb.shift();
  breadPolyline.setLatLngs(breadcrumb);

  // Map overlay
  document.getElementById('ov-lat').textContent = lat.toFixed(6);
  document.getElementById('ov-lon').textContent = lon.toFixed(6);

  // State dot
  const state = row.state || 'IDLE';
  updateStatusUI(state);

  // Alerts on state change
  if (state !== _prevState) {
    if (state === 'COMPLETE')      toast('✅ Mission Complete!', 'success', 5000);
    if (state === 'GPS_LOST')      toast('⚠️ GPS signal lost!', 'error', 5000);
    if (state === 'GEOFENCE_STOP') toast('🚫 Geofence breach! Mission stopped.', 'error', 5000);
    if (state === 'DWELL')         toast(`📍 Dwell at WP ${(row.waypoint_index||0)+1}`, 'info', 3000);
    _prevState = state;
  }

  // Telemetry panel
  document.getElementById('t-heading').textContent = row.heading_deg != null ? `${row.heading_deg.toFixed(1)}°` : '—°';
  document.getElementById('t-speed').textContent   = row.speed_mps   != null ? `${row.speed_mps.toFixed(2)} m/s` : '— m/s';
  document.getElementById('t-sat').textContent     = row.satellites  ?? '—';
  document.getElementById('t-fix').textContent     = row.fix_quality ?? '—';
  document.getElementById('t-left').textContent    = row.left_power  ?? '—';
  document.getElementById('t-right').textContent   = row.right_power ?? '—';
  document.getElementById('t-bearing').textContent = row.target_bearing   != null ? `${row.target_bearing.toFixed(1)}°` : '—°';
  document.getElementById('t-dist').textContent    = row.distance_to_target != null ? `${row.distance_to_target.toFixed(1)} m` : '— m';
  document.getElementById('t-mode').textContent    = row.effective_mode ?? '—';

  // Waypoint index
  const wpIdx = row.waypoint_index ?? '—';
  const total = row.total_waypoints ?? '—';
  document.getElementById('stat-wp-idx').textContent = (wpIdx !== '—' && total !== '—') ? `${wpIdx+1}/${total}` : '—';

  // Sensors
  if (row.sensors && typeof row.sensors === 'object') {
    updateSensors(row.sensors);
  }
}

// ─── Sensor panel ─────────────────────────────────────────────────────────────
const SENSOR_CONFIG = {
  ph:    { id: 'sv-ph',    unit: '',    tile: 's-ph'    },
  tds:   { id: 'sv-tds',   unit: 'ppm', tile: 's-tds'   },
  turb:  { id: 'sv-turb',  unit: 'NTU', tile: 's-turb'  },
  wtemp: { id: 'sv-wtemp', unit: '°C',  tile: 's-wtemp' },
  atemp: { id: 'sv-atemp', unit: '°C',  tile: 's-atemp' },
  hum:   { id: 'sv-hum',   unit: '%',   tile: 's-hum'   },
};

function updateSensors(sensors) {
  for (const [key, cfg] of Object.entries(SENSOR_CONFIG)) {
    if (sensors[key] != null) {
      const el = document.getElementById(cfg.id);
      if (el) {
        el.innerHTML = `${sensors[key].toFixed(2)}<span class="sensor-unit"> ${cfg.unit}</span>`;
      }
      const tile = document.getElementById(cfg.tile);
      if (tile) {
        tile.classList.add('updated');
        setTimeout(() => tile.classList.remove('updated'), 600);
      }
    }
  }
}

// ─── Command sender ───────────────────────────────────────────────────────────
async function sendCommand(type, payload = {}) {
  if (!sb) { toast('Not connected to Supabase', 'error'); return; }
  try {
    const { error } = await sb.from('commands').insert({ type, payload });
    if (error) throw error;
    toast(`↑ ${type}`, 'info', 1500);
  } catch (err) {
    console.error('sendCommand failed:', err);
    toast(`Command failed: ${err.message}`, 'error');
  }
}

// ─── Mission controls ─────────────────────────────────────────────────────────
async function cmdStart() {
  if (waypoints.length === 0) {
    toast('No waypoints — add a path first', 'warn');
    return;
  }
  if (!sb) { toast('Not connected to Supabase', 'error'); return; }

  // 1) Persist the mission plan (missions + mission_waypoints) to Supabase,
  //    then 2) tell the Pi to fetch it and start. The Pi pulls the GPS points
  //    itself (start_mission → get_mission_waypoints).
  try {
    const { data: mission, error: mErr } = await sb
      .from('missions')
      .insert({
        name: `Mission ${new Date().toLocaleTimeString()}`,
        status: 'PLANNED',
        waypoint_count: waypoints.length,
        gcs_lat: gcsLatLon ? gcsLatLon[0] : null,
        gcs_lon: gcsLatLon ? gcsLatLon[1] : null,
      })
      .select()
      .single();
    if (mErr) throw mErr;

    const wpRows = waypoints.map((wp, i) => ({
      mission_id: mission.id, seq: i, lat: wp.lat, lon: wp.lon,
    }));
    const { error: wErr } = await sb.from('mission_waypoints').insert(wpRows);
    if (wErr) throw wErr;

    missionLoaded = true;
    await sendCommand('start_mission', { mission_id: mission.id });
    toast(`Mission #${mission.id} planned & started`, 'success', 4000);
  } catch (err) {
    console.error('cmdStart failed:', err);
    toast(`Failed to start mission: ${err.message}`, 'error');
  }
}

async function cmdPause()         { await sendCommand('pause'); }
async function cmdStop()          { await sendCommand('stop'); missionLoaded = false; }
async function cmdEmergencyStop() { await sendCommand('emergency_stop'); missionLoaded = false; toast('🚨 Emergency stop sent!', 'error', 4000); }

function onSpeedChange(val) {
  document.getElementById('speed-value').textContent = val;
  sendCommand('set_speed', { power: parseInt(val, 10) });
}

// ─── Layer / mode toggles ─────────────────────────────────────────────────────
function setLayer(name) {
  currentLayer = name;
  initLayer(name);
  document.getElementById('btn-street').classList.toggle('active', name === 'street');
  document.getElementById('btn-satellite').classList.toggle('active', name === 'satellite');
}

function toggleGcsMode() {
  if (pathMode) togglePathMode();
  gcsMode = !gcsMode;
  document.getElementById('btn-set-gcs').classList.toggle('active', gcsMode);
  document.body.classList.toggle('mode-gcs', gcsMode);
  if (gcsMode) toast('Click on the map to place GCS', 'info', 3000);
}

function togglePathMode() {
  if (gcsMode) {
    gcsMode = false;
    document.getElementById('btn-set-gcs').classList.remove('active');
    document.body.classList.remove('mode-gcs');
  }
  if (pathMode && activePathId !== null) {
    // End current path
    pathMode = false; activePathId = null;
    document.getElementById('btn-add-path').classList.remove('active');
    document.body.classList.remove('mode-path');
    toast('Path complete — click START to run mission', 'success');
  } else {
    // New path
    const newPath = { id: Date.now(), waypoints: [] };
    paths.push(newPath);
    activePathId = newPath.id;
    pathMode = true;
    document.getElementById('btn-add-path').classList.add('active');
    document.body.classList.add('mode-path');
    renderPathList();
    toast('Click on map to add waypoints. Click + Add Path again to finish.', 'info', 4000);
  }
}

// ─── Supabase Realtime subscription ──────────────────────────────────────────
function subscribeToTelemetry() {
  if (!sb) return;

  sb.channel('telemetry-live')
    .on(
      'postgres_changes',
      { event: 'INSERT', schema: 'public', table: 'telemetry' },
      (payload) => {
        if (payload.new) onTelemetry(payload.new);
      }
    )
    .subscribe((status) => {
      console.log('[Supabase Realtime] status:', status);
      if (status === 'SUBSCRIBED') {
        toast('📡 Connected to Supabase Realtime', 'success');
      } else if (status === 'CLOSED' || status === 'CHANNEL_ERROR') {
        toast('Realtime connection lost — retrying...', 'warn');
        updatePiBadge(false);
      }
    });

  // Pi processing info (pi_system table) — live Raspberry Pi metrics
  sb.channel('pi-system-live')
    .on(
      'postgres_changes',
      { event: 'INSERT', schema: 'public', table: 'pi_system' },
      (payload) => {
        if (payload.new) onSystemInfo(payload.new);
      }
    )
    .subscribe((status) => {
      console.log('[pi_system Realtime] status:', status);
    });

  // Also load the latest telemetry row on connect (so map isn't empty on first load)
  loadLatestTelemetry();
  loadLatestSystemInfo();
}

async function loadLatestTelemetry() {
  if (!sb) return;
  try {
    const { data, error } = await sb
      .from('telemetry')
      .select('*')
      .order('id', { ascending: false })
      .limit(1);
    if (!error && data && data.length > 0) {
      onTelemetry(data[0]);
    }
  } catch (err) {
    console.warn('Could not load latest telemetry:', err);
  }
}

async function loadLatestSystemInfo() {
  if (!sb) return;
  try {
    const { data, error } = await sb
      .from('pi_system')
      .select('*')
      .order('id', { ascending: false })
      .limit(1);
    if (!error && data && data.length > 0) {
      onSystemInfo(data[0]);
    }
  } catch (err) {
    console.warn('Could not load latest pi_system:', err);
  }
}

// ─── Pi system info rendering ────────────────────────────────────────────────
function onSystemInfo(row) {
  const set = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };
  set('p-cpu',    row.cpu_pct    != null ? `${row.cpu_pct.toFixed(1)}%` : '—%');
  set('p-ram',    row.ram_pct    != null ? `${row.ram_pct.toFixed(1)}%` : '—%');
  set('p-temp',   row.cpu_temp_c != null ? `${row.cpu_temp_c.toFixed(1)}°C` : '—°C');
  set('p-uptime', row.uptime_s   != null ? formatUptime(row.uptime_s) : '—');
  set('p-navtick',row.nav_tick_ms!= null ? `${row.nav_tick_ms.toFixed(1)} ms` : '— ms');
  set('p-error',  row.last_error || '—');
}

function formatUptime(s) {
  if (s == null) return '—';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// ─── Mission history ──────────────────────────────────────────────────────────
async function loadHistory() {
  if (!sb) return;
  try {
    const { data, error } = await sb
      .from('missions')
      .select('*')
      .order('id', { ascending: false })
      .limit(20);
    if (error) throw error;

    const container = document.getElementById('history-list');
    if (!data || data.length === 0) {
      container.innerHTML = '<span style="color:var(--text-muted);font-size:11px">No missions yet</span>';
      return;
    }

    container.innerHTML = data.map(m => `
      <div class="history-item" onclick="loadMissionResults(${m.id})">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
          <span class="history-id">Mission #${m.id}</span>
          <span class="history-status status-${m.status}">${m.status}</span>
        </div>
        <div class="history-time">${new Date(m.created_at).toLocaleString()} · ${m.waypoint_count} WPs</div>
      </div>
    `).join('');

  } catch (err) {
    console.error('loadHistory failed:', err);
    toast(`History load failed: ${err.message}`, 'error');
  }
}

async function loadMissionResults(missionId) {
  if (!sb) return;
  try {
    const { data, error } = await sb
      .from('waypoint_results')
      .select('*')
      .eq('mission_id', missionId)
      .order('waypoint_index');
    if (error) throw error;
    if (!data || data.length === 0) {
      toast(`No results for mission #${missionId}`, 'info');
      return;
    }

    // Clear old dwell markers and place new ones for this historical mission
    dwellMarkers.forEach(m => map.removeLayer(m));
    dwellMarkers = [];

    data.forEach(r => {
      if (r.lat && r.lon) {
        const sensors = r.avg_sensors || {};
        const sensorStr = Object.entries(sensors)
          .map(([k,v]) => `${k}: ${v}`)
          .join('<br>');
        const m = L.marker([r.lat, r.lon], {
          icon: L.divIcon({ className:'', html:'<div class="dwell-marker"></div>', iconSize:[18,18], iconAnchor:[9,9] }),
        }).addTo(map).bindPopup(`
          <b>WP ${r.waypoint_index+1} — Mission #${missionId}</b><br>
          Samples: ${r.sample_count}<br>
          ${sensorStr || '—'}
        `);
        dwellMarkers.push(m);
      }
    });

    // Fly to first result
    if (data[0] && data[0].lat) {
      map.flyTo([data[0].lat, data[0].lon], 16, { duration: 1.2 });
    }

    toast(`Loaded ${data.length} WP results for mission #${missionId}`, 'success');

  } catch (err) {
    console.error('loadMissionResults failed:', err);
    toast(`Failed to load results: ${err.message}`, 'error');
  }
}

// ─── Sensor readings history (bath-uploaded every 2 min) ───────────────────
async function loadSensorHistory(missionId) {
  if (!sb) return;
  // Latest mission by default
  if (missionId == null) {
    const { data: latest } = await sb.from('missions').select('id').order('id', { ascending: false }).limit(1);
    if (!latest || latest.length === 0) { toast('No missions yet', 'info'); return; }
    missionId = latest[0].id;
  }
  try {
    const { data, error } = await sb
      .from('sensor_readings')
      .select('*')
      .eq('mission_id', missionId)
      .order('id', { ascending: true });
    if (error) throw error;

    // Clear old dwell markers and place sensor-reading markers
    dwellMarkers.forEach(m => map.removeLayer(m));
    dwellMarkers = [];

    if (!data || data.length === 0) {
      toast(`No sensor readings for mission #${missionId} yet (uploads every 2 min)`, 'info');
      return;
    }

    data.forEach(r => {
      if (r.lat && r.lon) {
        const sensors = r.sensors || {};
        const sensorStr = Object.entries(sensors).map(([k,v]) => `${k}: ${v}`).join('<br>');
        const m = L.marker([r.lat, r.lon], {
          icon: L.divIcon({ className:'', html:'<div class="dwell-marker"></div>', iconSize:[18,18], iconAnchor:[9,9] }),
        }).addTo(map).bindPopup(`
          <b>Mission #${missionId} — ${new Date(r.inserted_at).toLocaleString()}</b><br>
          ${sensorStr || '—'}
        `);
        dwellMarkers.push(m);
      }
    });

    if (data[0] && data[0].lat) {
      map.flyTo([data[0].lat, data[0].lon], 16, { duration: 1.2 });
    }
    toast(`Loaded ${data.length} sensor readings for mission #${missionId}`, 'success');

  } catch (err) {
    console.error('loadSensorHistory failed:', err);
    toast(`Failed to load sensor history: ${err.message}`, 'error');
  }
}

// ─── Freshness polling ────────────────────────────────────────────────────────
freshnessTimer = setInterval(checkFreshness, 3000);

// ─── Init ────────────────────────────────────────────────────────────────────
function init() {
  updateStats();
  updateStatusUI('IDLE');
  updatePiBadge(false);

  const ok = initSupabase();
  if (ok) {
    subscribeToTelemetry();
    loadHistory();
  }
}

init();
