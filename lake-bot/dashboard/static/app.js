/**
 * app.js — Lake Bot Mission Planner frontend logic
 *
 * Vanilla JS + Leaflet. No build step, no frameworks.
 * WebSocket connects to the dashboard server's own /ws endpoint.
 *
 * Responsibilities:
 *   • WebSocket client: receive telemetry, send commands
 *   • Leaflet map: bot marker (rotating arrow), planned path polyline,
 *     live breadcrumb polyline, waypoint markers, GCS marker
 *   • Map layer switching (OpenStreetMap ↔ Esri World Imagery)
 *   • Mission planning: GCS placement, waypoint path building
 *   • UI binding: status dot, state label, telemetry panel, stats tiles
 *   • Speed slider, simulation control buttons
 */

'use strict';

// ─── Configuration ───────────────────────────────────────────────────
const MAP_CENTER = [12.91686, 77.48698];
const MAP_ZOOM   = 17;
const WS_URL     = `ws://${location.host}/ws`;

const TILE_LAYERS = {
  street: {
    url:   'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    attr:  '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZ:  19,
  },
  satellite: {
    url:   'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attr:  'Tiles © Esri — Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community',
    maxZ:  18,
  },
};

// ─── State ───────────────────────────────────────────────────────────
let ws          = null;
let currentLayer = 'street';
let leafletLayer = null;

// GCS
let gcsLatLon    = null;   // [lat, lon]
let gcsMarker    = null;
let gcsMode      = false;

// Mission waypoints (list of {lat, lon})
let waypoints    = [];
let wpMarkers    = [];
let planPolyline = null;   // dashed teal planned path

// Bot marker & breadcrumb
let botMarker    = null;
let botLatLon    = [MAP_CENTER[0], MAP_CENTER[1]];
let breadcrumb   = [];     // array of [lat, lon]
let breadPolyline = null;  // solid orange live trace

// Path list (segments)
let paths        = [];     // [ {id, waypoints: [...]} ]
let pathMode     = false;
let activePathId = null;

// Session flag: whether load_mission was sent this session
let missionSentThisSession = false;

// ─── Haversine (client-side distance calc) ───────────────────────────
function haversineM(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * Math.PI / 180) *
    Math.cos(lat2 * Math.PI / 180) *
    Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function totalMissionDistance() {
  if (waypoints.length < 2) return 0;
  let d = 0;
  for (let i = 0; i < waypoints.length - 1; i++) {
    d += haversineM(waypoints[i].lat, waypoints[i].lon,
                    waypoints[i+1].lat, waypoints[i+1].lon);
  }
  return d;
}

// ─── Map initialisation ───────────────────────────────────────────────
const map = L.map('map', {
  center: MAP_CENTER,
  zoom:   MAP_ZOOM,
  zoomControl: true,
});

function initLayer(name) {
  const cfg = TILE_LAYERS[name];
  if (leafletLayer) map.removeLayer(leafletLayer);
  leafletLayer = L.tileLayer(cfg.url, { attribution: cfg.attr, maxZoom: cfg.maxZ });
  leafletLayer.addTo(map);
}

initLayer('street');

// ─── Bot marker (rotating SVG arrow) ─────────────────────────────────
function makeBotIcon(headingDeg) {
  return L.divIcon({
    className: '',
    html: `
      <div style="
        width:36px; height:36px;
        transform: rotate(${headingDeg}deg);
        transform-origin: center;
        display:flex; align-items:center; justify-content:center;
      ">
        <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
          <circle cx="18" cy="18" r="16" fill="#0d1117" stroke="#2dd4a7" stroke-width="2"/>
          <path d="M18 6 L24 28 L18 24 L12 28 Z" fill="#2dd4a7"/>
        </svg>
      </div>`,
    iconSize:   [36, 36],
    iconAnchor: [18, 18],
  });
}

botMarker = L.marker(MAP_CENTER, { icon: makeBotIcon(0), zIndexOffset: 1000 })
  .addTo(map)
  .bindTooltip('BOT', { permanent: false, direction: 'top' });

// Breadcrumb polyline (orange, solid)
breadPolyline = L.polyline([], {
  color:     '#f97316',
  weight:    2,
  opacity:   0.85,
}).addTo(map);

// Planned path polyline (teal, dashed)
planPolyline = L.polyline([], {
  color:       '#2dd4a7',
  weight:      2,
  opacity:     0.7,
  dashArray:   '6 6',
}).addTo(map);

// ─── GCS marker helpers ───────────────────────────────────────────────
function placeGcsMarker(lat, lon) {
  if (gcsMarker) map.removeLayer(gcsMarker);
  gcsMarker = L.marker([lat, lon], {
    icon: L.divIcon({
      className: '',
      html: '<div class="gcs-marker">GCS</div>',
      iconSize:   [28, 28],
      iconAnchor: [14, 14],
    }),
  }).addTo(map).bindTooltip(`GCS: ${lat.toFixed(5)}, ${lon.toFixed(5)}`, { direction: 'top' });
}

// ─── Waypoint marker helpers ──────────────────────────────────────────
function rebuildWpMarkers() {
  wpMarkers.forEach(m => map.removeLayer(m));
  wpMarkers = [];

  waypoints.forEach((wp, i) => {
    const m = L.marker([wp.lat, wp.lon], {
      icon: L.divIcon({
        className: '',
        html: `<div class="wp-marker">${i + 1}</div>`,
        iconSize:   [22, 22],
        iconAnchor: [11, 11],
      }),
    }).addTo(map)
      .bindTooltip(`WP ${i + 1}: ${wp.lat.toFixed(5)}, ${wp.lon.toFixed(5)}`, { direction: 'top' });
    wpMarkers.push(m);
  });

  // Update plan polyline
  planPolyline.setLatLngs(waypoints.map(wp => [wp.lat, wp.lon]));
}

// ─── Path list (sidebar) ──────────────────────────────────────────────
function renderPathList() {
  const ul = document.getElementById('path-list');
  ul.innerHTML = '';

  paths.forEach((path, idx) => {
    const li = document.createElement('li');
    li.className = 'path-item';
    li.id = `path-item-${path.id}`;
    li.innerHTML = `
      <span class="path-item-name">Path ${idx + 1} <span style="color:var(--text-dim);font-size:9px">(${path.waypoints.length} pts)</span></span>
      <div class="path-controls">
        <button class="path-btn" title="Move up" onclick="movePath(${idx}, -1)">▲</button>
        <button class="path-btn" title="Move down" onclick="movePath(${idx}, 1)">▼</button>
        <button class="path-btn del" title="Delete" onclick="deletePath(${idx})">×</button>
      </div>`;
    ul.appendChild(li);
  });

  // Flatten all path waypoints into global waypoints array
  waypoints = paths.flatMap(p => p.waypoints);
  rebuildWpMarkers();
  updateStats();
}

function movePath(idx, dir) {
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= paths.length) return;
  [paths[idx], paths[newIdx]] = [paths[newIdx], paths[idx]];
  renderPathList();
}

function deletePath(idx) {
  const path = paths[idx];
  if (activePathId === path.id) {
    pathMode = false;
    activePathId = null;
    document.getElementById('btn-add-path').classList.remove('active');
    document.body.classList.remove('mode-path');
  }
  paths.splice(idx, 1);
  renderPathList();
  missionSentThisSession = false;
}

// ─── UI update helpers ────────────────────────────────────────────────
function updateStats() {
  document.getElementById('stat-waypoints').textContent = waypoints.length;

  const dist = totalMissionDistance();
  document.getElementById('stat-distance').textContent =
    dist > 1000 ? `${(dist / 1000).toFixed(2)} km` : `${Math.round(dist)} m`;

  document.getElementById('stat-gcs').textContent = gcsLatLon ? 'SET' : 'UNSET';
}

const STATE_DOT_CLASSES = {
  IDLE:          'dot-idle',
  RUNNING:       'dot-running',
  PAUSED:        'dot-paused',
  COMPLETE:      'dot-complete',
  STOPPED:       'dot-stopped',
  GEOFENCE_STOP: 'dot-geofence',
  GPS_LOST:      'dot-gpslost',
};

function updateStatusUI(state) {
  const dot   = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  dot.className = STATE_DOT_CLASSES[state] || 'dot-idle';
  label.textContent = state;
}

function updatePiBadge(online) {
  const badge = document.getElementById('pi-badge');
  const text  = document.getElementById('pi-badge-text');
  badge.className = online ? 'badge-online' : 'badge-offline';
  text.textContent = online ? 'PI ONLINE' : 'PI OFFLINE';
}

// ─── Telemetry handler ────────────────────────────────────────────────
function onTelemetry(msg) {
  const { lat, lon, heading_deg, speed_mps, state,
          current_waypoint_index, satellites, fix_quality,
          left_power, right_power } = msg;

  // Bot marker
  const newLatLon = [lat, lon];
  botLatLon = newLatLon;
  botMarker.setLatLng(newLatLon);
  botMarker.setIcon(makeBotIcon(heading_deg));

  // Breadcrumb
  breadcrumb.push(newLatLon);
  breadPolyline.setLatLngs(breadcrumb);

  // Status
  updateStatusUI(state);

  // Telemetry panel
  document.getElementById('t-heading').textContent = `${heading_deg.toFixed(1)}°`;
  document.getElementById('t-speed').textContent   = `${speed_mps.toFixed(2)} m/s`;
  document.getElementById('t-sat').textContent     = satellites;
  document.getElementById('t-fix').textContent     = fix_quality;
  document.getElementById('t-left').textContent    = left_power;
  document.getElementById('t-right').textContent   = right_power;
  document.getElementById('t-wp').textContent      = current_waypoint_index;
}

// ─── WebSocket ────────────────────────────────────────────────────────
function connectWs() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log('[WS] Connected to dashboard server');
  };

  ws.onmessage = (evt) => {
    let msg;
    try {
      msg = JSON.parse(evt.data);
    } catch (e) {
      console.error('[WS] Malformed message', e);
      return;
    }

    if (msg.type === 'telemetry') {
      onTelemetry(msg);
    } else if (msg.type === 'pi_connection') {
      updatePiBadge(msg.status === 'online');
    } else if (msg.type === 'error') {
      console.warn('[Pi error]', msg.message);
    }
  };

  ws.onclose = () => {
    console.warn('[WS] Disconnected. Reconnecting in 3s…');
    updatePiBadge(false);
    setTimeout(connectWs, 3000);
  };

  ws.onerror = (e) => console.error('[WS] Error', e);
}

function sendCmd(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn('[WS] Not connected — command dropped');
    return;
  }
  ws.send(JSON.stringify(obj));
}

// ─── Map click handler ────────────────────────────────────────────────
map.on('click', (e) => {
  const { lat, lng: lon } = e.latlng;

  if (gcsMode) {
    // Place GCS
    gcsLatLon = [lat, lon];
    placeGcsMarker(lat, lon);

    document.getElementById('gcs-lat-val').textContent = lat.toFixed(6);
    document.getElementById('gcs-lon-val').textContent = lon.toFixed(6);
    document.getElementById('gcs-info').classList.remove('hidden');
    updateStats();

    sendCmd({ type: 'set_gcs', lat, lon });

    // Auto-exit GCS mode after placement
    gcsMode = false;
    document.getElementById('btn-set-gcs').classList.remove('active');
    document.body.classList.remove('mode-gcs');

  } else if (pathMode && activePathId !== null) {
    // Add waypoint to active path
    const path = paths.find(p => p.id === activePathId);
    if (path) {
      path.waypoints.push({ lat, lon });
      renderPathList();
      missionSentThisSession = false;
    }
  }
});

// ─── Layer toggle ─────────────────────────────────────────────────────
function setLayer(name) {
  currentLayer = name;
  initLayer(name);
  document.getElementById('btn-street').classList.toggle('active', name === 'street');
  document.getElementById('btn-satellite').classList.toggle('active', name === 'satellite');
}

// ─── GCS mode toggle ─────────────────────────────────────────────────
function toggleGcsMode() {
  // Exit path mode if active
  if (pathMode) togglePathMode();

  gcsMode = !gcsMode;
  document.getElementById('btn-set-gcs').classList.toggle('active', gcsMode);
  document.body.classList.toggle('mode-gcs', gcsMode);
}

// ─── Path mode toggle ─────────────────────────────────────────────────
function togglePathMode() {
  // Exit GCS mode if active
  if (gcsMode) {
    gcsMode = false;
    document.getElementById('btn-set-gcs').classList.remove('active');
    document.body.classList.remove('mode-gcs');
  }

  if (pathMode && activePathId !== null) {
    // End current path
    pathMode = false;
    activePathId = null;
    document.getElementById('btn-add-path').classList.remove('active');
    document.body.classList.remove('mode-path');
  } else {
    // Start new path
    const newPath = { id: Date.now(), waypoints: [] };
    paths.push(newPath);
    activePathId = newPath.id;
    pathMode = true;
    document.getElementById('btn-add-path').classList.add('active');
    document.body.classList.add('mode-path');
    renderPathList();
  }
}

// ─── Speed slider ─────────────────────────────────────────────────────
function onSpeedChange(val) {
  document.getElementById('speed-value').textContent = val;
  sendCmd({ type: 'set_speed', power: parseInt(val, 10) });
}

// ─── Simulation controls ──────────────────────────────────────────────
function cmdStart() {
  if (!missionSentThisSession && waypoints.length > 0) {
    // Send load_mission first
    sendCmd({ type: 'load_mission', waypoints });
    missionSentThisSession = true;
  }
  sendCmd({ type: 'start' });
}

function cmdPause() {
  sendCmd({ type: 'pause' });
}

function cmdStop() {
  sendCmd({ type: 'stop' });
}

// ─── Init ─────────────────────────────────────────────────────────────
updateStats();
updateStatusUI('IDLE');
updatePiBadge(false);
connectWs();
