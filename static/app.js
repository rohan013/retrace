/* Day view: map on the right, chronological feed on the left, kept in sync.
 *
 * The raw-fixes toggle is the point of this UI rather than a debug aid. Seeing
 * every individual fix with its accuracy circle is how you judge whether a stay
 * is real or an artefact of bad reception, which is exactly what a tracker that
 * hides its raw data cannot let you do.
 */

const el = (id) => document.getElementById(id);

const state = {
  date: todayISO(),
  device: "",
  showRaw: false,
  day: null,
  layers: { track: null, stays: null, raw: null },
  markers: new Map(),
};

const map = L.map("map", { zoomControl: true }).setView([51.5074, -0.1278], 12);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

/* -- helpers -------------------------------------------------------------- */

function todayISO() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 10);
}

function shiftDate(iso, days) {
  const d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function clockTime(ts, tz) {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: tz || undefined,
  });
}

function duration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}

function distance(metres) {
  return metres >= 1000
    ? `${(metres / 1000).toFixed(1)} km`
    : `${Math.round(metres)} m`;
}

const SPEED_SATURATION_MPS = 25; // 90 km/h — the top of the colour ramp
const SPEED_BUCKETS = 8;

// Blue through green and yellow to red as speed rises.
function speedColour(mps) {
  const hue = 210 - 210 * Math.min((mps || 0) / SPEED_SATURATION_MPS, 1);
  return `hsl(${hue} 80% 48%)`;
}

function speedBucket(mps) {
  const share = Math.min((mps || 0) / SPEED_SATURATION_MPS, 1);
  return Math.min(SPEED_BUCKETS - 1, Math.floor(share * SPEED_BUCKETS));
}

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
}

/* -- rendering ------------------------------------------------------------ */

function clearLayers() {
  for (const key of Object.keys(state.layers)) {
    if (state.layers[key]) {
      map.removeLayer(state.layers[key]);
      state.layers[key] = null;
    }
  }
  state.markers.clear();
}

function renderSummary(day) {
  const s = day.summary;
  const stats = [
    ["Distance", distance(s.distance_m)],
    ["Moving", duration(s.time_moving_s)],
    ["Stays", String(s.stay_count)],
    ["Stationary", duration(s.time_stationary_s)],
  ];
  el("summary").innerHTML = stats
    .map(
      ([label, value]) =>
        `<div class="stat"><div class="value">${value}</div><div class="label">${label}</div></div>`
    )
    .join("");
}

function renderTimeline(day) {
  const list = el("timeline");
  list.innerHTML = "";
  el("empty").hidden = day.items.length > 0;

  for (const item of day.items) {
    const li = document.createElement("li");
    li.className = `item ${item.type}`;
    li.dataset.key = `${item.type}-${item.id}`;

    const when = `${clockTime(item.visible_start_ts, day.tz)}`;
    const carried = item.continuation_of
      ? `<span class="badge">from ${item.continuation_of}</span>`
      : "";

    if (item.type === "stay") {
      const name = item.name
        ? `<span>${escapeHTML(item.name)}</span>`
        : `<span class="unnamed">Unnamed place</span>`;
      const gap = item.had_gap ? `<span class="badge gap">gap</span>` : "";
      const low =
        item.confidence < 40 ? `<span class="badge low">low confidence</span>` : "";
      li.innerHTML = `
        <div class="when">${when}</div>
        <div>
          <div class="title">${name}${carried}</div>
          <div class="detail">
            ${duration(item.visible_duration_s)} &middot; ${item.point_count} fixes
            &middot; ${Math.round(item.radius_m)} m ${gap}${low}
          </div>
        </div>`;
      li.addEventListener("dblclick", () => renameStay(item));
    } else {
      li.innerHTML = `
        <div class="when">${when}</div>
        <div>
          <div class="title">Moving${carried}</div>
          <div class="detail">
            ${distance(item.distance_m)} &middot; ${duration(item.visible_duration_s)}
            ${item.max_speed ? `&middot; peak ${Math.round(item.max_speed * 3.6)} km/h` : ""}
          </div>
        </div>`;
    }

    li.addEventListener("mouseenter", () => highlight(li.dataset.key, true));
    li.addEventListener("mouseleave", () => highlight(li.dataset.key, false));
    li.addEventListener("click", () => focusItem(item));
    list.appendChild(li);
  }
}

function escapeHTML(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function renderStays(day) {
  const group = L.layerGroup();
  for (const item of day.items.filter((i) => i.type === "stay")) {
    // Longer stays read as larger; low confidence reads as fainter.
    const radius = Math.max(9, Math.min(26, 9 + Math.log1p(item.duration_s / 600) * 5));
    const opacity = 0.25 + 0.55 * ((item.confidence ?? 50) / 100);

    const marker = L.circleMarker([item.lat, item.lon], {
      radius,
      color: "#7c3aed",
      weight: 2,
      fillColor: "#7c3aed",
      fillOpacity: opacity,
    });

    // The clock times are this day's slice of the stay, so the duration beside
    // them has to be the slice too. A stay carried over from yesterday shows its
    // full span on a second line rather than silently mixing the two.
    const spansDays = item.visible_duration_s !== item.duration_s;

    marker.bindPopup(
      `<strong>${escapeHTML(item.name || "Unnamed place")}</strong><br>` +
        `${clockTime(item.visible_start_ts, day.tz)}–${clockTime(item.visible_end_ts, day.tz)}` +
        ` (${duration(item.visible_duration_s)})<br>` +
        (spansDays ? `${duration(item.duration_s)} in total<br>` : "") +
        `${item.point_count} fixes, ${Math.round(item.radius_m)} m radius<br>` +
        `confidence ${item.confidence}` +
        (item.had_gap ? " &middot; includes a reporting gap" : "")
    );

    // Also show the stay's actual extent, not just a fixed dot.
    L.circle([item.lat, item.lon], {
      radius: item.radius_m,
      color: "#7c3aed",
      weight: 1,
      opacity: 0.35,
      fill: false,
      dashArray: "3 4",
    }).addTo(group);

    marker.addTo(group);
    state.markers.set(`stay-${item.id}`, marker);
  }
  group.addTo(map);
  state.layers.stays = group;
}

function renderTrack(points) {
  const group = L.layerGroup();

  // One polyline per *run* of similar speed, not per pair of points. A real day
  // is three to six thousand fixes, and that many separate SVG paths is what
  // makes a map crawl when you pan it. The colour ramp is quantised anyway, so
  // merging runs costs nothing visually. Runs share their endpoint, so the line
  // stays continuous across a colour change.
  let run = [];
  let bucket = null;
  let dashed = null;

  const flush = () => {
    if (run.length < 2) return;
    L.polyline(
      run.map((p) => [p.lat, p.lon]),
      {
        color: speedColour(((bucket + 0.5) / SPEED_BUCKETS) * SPEED_SATURATION_MPS),
        weight: 4,
        opacity: 0.85,
        dashArray: dashed ? "5 7" : null,
      }
    ).addTo(group);
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

  group.addTo(map);
  state.layers.track = group;
}

function renderRaw(points) {
  const group = L.layerGroup();
  for (const p of points) {
    const flagged = Boolean(p.anomaly);
    if (p.accuracy) {
      L.circle([p.lat, p.lon], {
        radius: p.accuracy,
        color: flagged ? "#dc2626" : "#2563eb",
        weight: 1,
        opacity: 0.3,
        fillOpacity: 0.05,
      }).addTo(group);
    }
    L.circleMarker([p.lat, p.lon], {
      radius: 2.5,
      color: flagged ? "#dc2626" : "#2563eb",
      fillOpacity: 1,
      weight: flagged ? 2 : 0,
    })
      .bindPopup(
        `${new Date(p.ts * 1000).toLocaleString()}<br>` +
          `accuracy ${p.accuracy ?? "?"} m &middot; ${p.trigger_type ?? "?"}` +
          (p.battery ? `<br>battery ${p.battery}%` : "") +
          (flagged ? `<br><strong>flagged: ${p.anomaly_reason}</strong>` : "")
      )
      .addTo(group);
  }
  group.addTo(map);
  state.layers.raw = group;
}

function haversine(a, b) {
  const R = 6371008.8;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

/* -- interaction ---------------------------------------------------------- */

function highlight(key, on) {
  const marker = state.markers.get(key);
  if (marker) marker.setStyle({ weight: on ? 5 : 2 });
  document
    .querySelectorAll(".item")
    .forEach((node) => node.classList.toggle("active", on && node.dataset.key === key));
}

function focusItem(item) {
  if (item.type === "stay") {
    map.setView([item.lat, item.lon], Math.max(map.getZoom(), 16));
    state.markers.get(`stay-${item.id}`)?.openPopup();
  }
}

async function renameStay(item) {
  const name = prompt("Name this place", item.name || "");
  if (name === null || !name.trim()) return;
  await fetch(`/api/v1/stays/${item.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  await load();
}

/* -- loading -------------------------------------------------------------- */

async function loadDevices() {
  const devices = await getJSON("/api/v1/devices");
  const select = el("device");
  select.innerHTML = '<option value="">All devices</option>';
  for (const d of devices) {
    const option = document.createElement("option");
    option.value = d.device;
    option.textContent = `${d.device} (${d.points.toLocaleString()})`;
    select.appendChild(option);
  }
  select.value = state.device;
}

async function loadPoints(day) {
  const params = new URLSearchParams({
    from: day.start_ts,
    to: day.end_ts,
    limit: "50000",
  });
  if (state.device) params.set("device", state.device);

  let since = 0;
  const points = [];
  for (let page = 0; page < 40; page++) {
    params.set("since_id", String(since));
    const chunk = await getJSON(`/api/v1/points?${params}`);
    points.push(...chunk.points);
    if (chunk.complete) break;
    since = chunk.next_since_id;
  }
  return points;
}

async function load() {
  const params = new URLSearchParams();
  if (state.device) params.set("device", state.device);
  const day = await getJSON(`/api/v1/days/${state.date}?${params}`);
  state.day = day;

  clearLayers();
  renderSummary(day);
  renderTimeline(day);
  renderStays(day);

  const points = await loadPoints(day);
  const trusted = points.filter((p) => !p.anomaly);
  if (trusted.length > 1) renderTrack(trusted);
  if (state.showRaw && points.length) renderRaw(points);

  const bounds = [];
  for (const p of trusted) bounds.push([p.lat, p.lon]);
  for (const item of day.items.filter((i) => i.type === "stay"))
    bounds.push([item.lat, item.lon]);
  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
}

/* -- wiring --------------------------------------------------------------- */

el("date").value = state.date;
el("date").addEventListener("change", (e) => {
  state.date = e.target.value;
  load();
});
el("prev").addEventListener("click", () => {
  state.date = shiftDate(state.date, -1);
  el("date").value = state.date;
  load();
});
el("next").addEventListener("click", () => {
  state.date = shiftDate(state.date, 1);
  el("date").value = state.date;
  load();
});
el("today").addEventListener("click", () => {
  state.date = todayISO();
  el("date").value = state.date;
  load();
});
el("device").addEventListener("change", (e) => {
  state.device = e.target.value;
  load();
});
el("show-raw").addEventListener("change", (e) => {
  state.showRaw = e.target.checked;
  load();
});

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "ArrowLeft") el("prev").click();
  if (e.key === "ArrowRight") el("next").click();
});

loadDevices().then(load).catch((err) => {
  el("empty").hidden = false;
  el("empty").textContent = `Could not load: ${err.message}`;
});
