/* Leaflet rendering: the track, stays, raw fixes, areas and the scrub
 * marker. The map itself stays a compact card in the right column — every
 * path here is unchanged from the original sidebar-map version except
 * renderTrack(), which now draws one polyline set per device instead of one
 * line that could zig-zag between two devices' positions.
 */

import { clockTime, distance, duration, escapeHTML, haversine, placeHue, speedBucket, speedColour, SPEED_BUCKETS, SPEED_SATURATION_MPS } from "./format.js";

export const map = L.map("map", { zoomControl: true }).setView([51.5074, -0.1278], 12);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const layers = { tracks: null, stays: null, raw: null, scrub: null };
const markers = new Map();

export function clearDayLayers() {
  for (const key of ["tracks", "stays", "raw"]) {
    if (layers[key]) {
      map.removeLayer(layers[key]);
      layers[key] = null;
    }
  }
  markers.clear();
}

const AREA_STYLE = {
  color: "#0d9488",
  weight: 2,
  fillColor: "#0d9488",
  fillOpacity: 0.1,
  dashArray: "4 4",
};

/* -- stays ------------------------------------------------------------------ */

export function renderStays(day, { onRename } = {}) {
  const group = L.layerGroup();
  for (const item of day.items.filter((i) => i.type === "stay")) {
    const hue = placeHue(item);
    // Longer stays read as larger; low confidence reads as fainter.
    const radius = Math.max(9, Math.min(26, 9 + Math.log1p(item.duration_s / 600) * 5));
    const opacity = 0.25 + 0.55 * ((item.confidence ?? 50) / 100);

    const marker = L.circleMarker([item.lat, item.lon], {
      radius,
      color: hue,
      weight: 2,
      fillColor: hue,
      fillOpacity: opacity,
    });

    // The clock times are this day's slice of the stay, so the duration beside
    // them has to be the slice too. A stay carried over from yesterday shows its
    // full span on a second line rather than silently mixing the two.
    const spansDays = item.visible_duration_s !== item.duration_s;

    const popupId = `rename-${item.id}`;
    marker.bindPopup(
      `<strong>${escapeHTML(item.name || "Unnamed place")}</strong><br>` +
        `${clockTime(item.visible_start_ts, day.tz)}–${clockTime(item.visible_end_ts, day.tz)}` +
        ` (${duration(item.visible_duration_s)})<br>` +
        (spansDays ? `${duration(item.duration_s)} in total<br>` : "") +
        `${item.point_count} fixes, ${Math.round(item.radius_m)} m radius<br>` +
        `confidence ${item.confidence}` +
        (item.had_gap ? " &middot; includes a reporting gap" : "") +
        `<br><a href="#" class="popup-rename" data-id="${popupId}">Rename</a>`
    );
    marker.on("popupopen", (e) => {
      e.popup
        .getElement()
        ?.querySelector(`[data-id="${popupId}"]`)
        ?.addEventListener("click", (ev) => {
          ev.preventDefault();
          onRename?.(item);
        });
    });

    // Also show the stay's actual extent, not just a fixed dot.
    L.circle([item.lat, item.lon], {
      radius: item.radius_m,
      color: hue,
      weight: 1,
      opacity: 0.35,
      fill: false,
      dashArray: "3 4",
    }).addTo(group);

    marker.addTo(group);
    markers.set(`stay-${item.id}`, marker);
  }
  group.addTo(map);
  layers.stays = group;
}

/* -- track -------------------------------------------------------------------
 * One polyline series per device: a single line through every point
 * regardless of device would jump between two devices' positions on any day
 * both reported fixes. Points are already loaded once per day and grouped
 * here rather than re-fetched per device.
 */

// A device's own colour only matters once a second device shows up; until
// then the speed ramp alone (identical either way) carries the track.
const DEVICE_TINTS = ["#3987e5", "#9085e9", "#199e70", "#d95926"];
function deviceTint(device, deviceOrder) {
  const idx = deviceOrder.indexOf(device);
  return DEVICE_TINTS[idx % DEVICE_TINTS.length];
}

export function renderTrack(points) {
  const group = L.layerGroup();
  const byDevice = new Map();
  for (const p of points) {
    const key = p.device || "";
    if (!byDevice.has(key)) byDevice.set(key, []);
    byDevice.get(key).push(p);
  }
  const deviceOrder = [...byDevice.keys()];
  const multiDevice = deviceOrder.length > 1;

  for (const [device, devicePoints] of byDevice) {
    devicePoints.sort((a, b) => a.ts - b.ts);
    renderDeviceTrack(group, devicePoints, multiDevice ? deviceTint(device, deviceOrder) : null);
  }

  group.addTo(map);
  layers.tracks = group;
}

// One polyline per *run* of similar speed, not per pair of points. A real day
// is three to six thousand fixes, and that many separate SVG paths is what
// makes a map crawl when you pan it. Runs share their endpoint, so the line
// stays continuous across a colour change.
function renderDeviceTrack(group, points, tint) {
  let run = [];
  let bucket = null;
  let dashed = null;

  const flush = () => {
    if (run.length < 2) return;
    const base = speedColour(((bucket + 0.5) / SPEED_BUCKETS) * SPEED_SATURATION_MPS);
    L.polyline(
      run.map((p) => [p.lat, p.lon]),
      { color: base, weight: 4, opacity: 0.85, dashArray: dashed ? "5 7" : null }
    ).addTo(group);
    // A device tint rides as a thin outline rather than replacing the speed
    // colour, so "which device" and "how fast" stay two separate channels.
    if (tint) {
      L.polyline(
        run.map((p) => [p.lat, p.lon]),
        { color: tint, weight: 1.5, opacity: 0.9, dashArray: dashed ? "5 7" : null }
      ).addTo(group);
    }
  };

  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    const gapSeconds = b.ts - a.ts;
    const speed = b.speed_mps ?? (gapSeconds > 0 ? haversine(a, b) / gapSeconds : 0);

    const segBucket = speedBucket(speed);
    // A long silence is drawn dashed: the route through it is inferred.
    const segDashed = gapSeconds > 900;

    if (bucket === null || segBucket !== bucket || segDashed !== dashed) {
      flush();
      run = [a];
      bucket = segBucket;
      dashed = segDashed;
    }
    run.push(b);
  }
  flush();
}

/* -- raw fixes ---------------------------------------------------------------- */

// Flagged (anomalous) fixes use the dataviz status-critical token, not the
// geofence lane's categorical red — a status meaning shouldn't borrow a
// category's color, even when they happen to look similar.
const RAW_OK = "#3987e5";
const RAW_FLAGGED = "#d03b3b";

export function renderRaw(points) {
  const group = L.layerGroup();
  for (const p of points) {
    const flagged = Boolean(p.anomaly);
    if (p.accuracy) {
      L.circle([p.lat, p.lon], {
        radius: p.accuracy,
        color: flagged ? RAW_FLAGGED : RAW_OK,
        weight: 1,
        opacity: 0.3,
        fillOpacity: 0.05,
      }).addTo(group);
    }
    L.circleMarker([p.lat, p.lon], {
      radius: 2.5,
      color: flagged ? RAW_FLAGGED : RAW_OK,
      fillOpacity: 1,
      weight: flagged ? 2 : 0,
    })
      .bindPopup(
        `${new Date(p.ts * 1000).toLocaleString()}<br>` +
          `${p.device ? `device ${escapeHTML(p.device)}<br>` : ""}` +
          `accuracy ${p.accuracy ?? "?"} m &middot; ${escapeHTML(p.trigger_type ?? "?")}` +
          (p.battery ? `<br>battery ${p.battery}%` : "") +
          (flagged ? `<br><strong>flagged: ${escapeHTML(p.anomaly_reason)}</strong>` : "")
      )
      .addTo(group);
  }
  group.addTo(map);
  layers.raw = group;
}

/* -- areas --------------------------------------------------------------------
 * Areas are map furniture, not part of a day's view — loaded once and left
 * alone by clearDayLayers(), unlike the track/stays/raw layers.
 */

const areasLayer = L.layerGroup().addTo(map);

function areaShapeLayer(area) {
  return L.rectangle(
    [
      [area.min_lat, area.min_lon],
      [area.max_lat, area.max_lon],
    ],
    AREA_STYLE
  );
}

export async function loadAreas() {
  const response = await fetch("/api/v1/areas");
  const areas = await response.json();
  areasLayer.clearLayers();
  for (const area of areas) {
    const shape = areaShapeLayer(area);
    shape.bindPopup(
      `<strong>${escapeHTML(area.name)}</strong><br>` +
        `<a href="#" class="popup-delete" data-area-id="${area.id}">Delete</a>`
    );
    shape.on("popupopen", (e) => {
      e.popup
        .getElement()
        .querySelector(".popup-delete")
        ?.addEventListener("click", async (ev) => {
          ev.preventDefault();
          await fetch(`/api/v1/areas/${area.id}`, { method: "DELETE" });
          await loadAreas();
        });
    });
    shape.addTo(areasLayer);
  }
}

// Click-drag to draw: mousedown sets the anchor corner/centre, mousemove shows
// a live preview, mouseup finalises and prompts for a name. Map dragging is
// disabled for the duration so the gesture draws instead of panning.
let drawing = null;

export const isDrawing = () => drawing !== null;

/** onButtonChange(active) toggles the "+ Area" button's state; onCreated()
 * runs after a new area is saved and the day should reload. */
export function startDrawing({ onButtonChange, onCreated }) {
  drawing = { start: null, preview: null, onButtonChange, onCreated };
  map.dragging.disable();
  map.getContainer().style.cursor = "crosshair";
  onButtonChange(true);
  map.on("mousedown", onDrawStart);
}

export function stopDrawing() {
  if (!drawing) return;
  if (drawing.preview) map.removeLayer(drawing.preview);
  const { onButtonChange } = drawing;
  drawing = null;
  map.dragging.enable();
  map.getContainer().style.cursor = "";
  onButtonChange(false);
  map.off("mousedown", onDrawStart);
  map.off("mousemove", onDrawMove);
  map.off("mouseup", onDrawEnd);
}

function onDrawStart(e) {
  drawing.start = e.latlng;
  map.on("mousemove", onDrawMove);
  map.on("mouseup", onDrawEnd);
}

function onDrawMove(e) {
  if (drawing.preview) map.removeLayer(drawing.preview);
  drawing.preview = L.rectangle([drawing.start, e.latlng], AREA_STYLE).addTo(map);
}

async function onDrawEnd(e) {
  map.off("mousemove", onDrawMove);
  map.off("mouseup", onDrawEnd);

  const finished = drawing;
  const end = e.latlng;
  stopDrawing();

  // A click with no drag produces a zero-size shape — not a usable area.
  if (map.distance(finished.start, end) < 3) return;

  const name = prompt("Name this area");
  if (!name || !name.trim()) return;

  const body = {
    name: name.trim(),
    min_lat: Math.min(finished.start.lat, end.lat),
    min_lon: Math.min(finished.start.lng, end.lng),
    max_lat: Math.max(finished.start.lat, end.lat),
    max_lon: Math.max(finished.start.lng, end.lng),
  };

  await fetch("/api/v1/areas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // Existing stays only pick up the new area on a rebuild.
  await fetch("/api/v1/reprocess", { method: "POST" });
  await loadAreas();
  await finished.onCreated?.();
}

/* -- interaction --------------------------------------------------------------- */

export function highlightMarker(key, on) {
  const marker = markers.get(key);
  if (marker) marker.setStyle({ weight: on ? 5 : 2 });
}

export function focusStay(item) {
  map.setView([item.lat, item.lon], Math.max(map.getZoom(), 16));
  markers.get(`stay-${item.id}`)?.openPopup();
}

export async function renameStay(item, onDone) {
  const name = prompt("Name this place", item.name || "");
  if (name === null || !name.trim()) return;
  await fetch(`/api/v1/stays/${item.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  await onDone?.();
}

/* -- scrub marker ---------------------------------------------------------- */

let scrubMarker = null;

/** Binary-search `points` (already sorted by ts) for the nearest fix to ts,
 * move the scrub marker there, and return the matched point (or null). */
export function scrubTo(points, ts) {
  if (!points.length) return null;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].ts < ts) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(points[lo - 1].ts - ts) < Math.abs(points[lo].ts - ts)) lo -= 1;
  const point = points[lo];

  if (!scrubMarker) {
    scrubMarker = L.circleMarker([point.lat, point.lon], {
      radius: 7,
      color: "#e8eef8",
      weight: 2,
      fillColor: "#3987e5",
      fillOpacity: 1,
      className: "scrub-marker",
    }).addTo(map);
  } else {
    scrubMarker.setLatLng([point.lat, point.lon]);
  }
  return point;
}

export function clearScrub() {
  if (scrubMarker) {
    map.removeLayer(scrubMarker);
    scrubMarker = null;
  }
}
