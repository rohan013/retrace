/* Entrypoint: state, data loading, and wiring. Every other module is either
 * pure (format.js, layout.js) or exposes an imperative API this file drives
 * (track.js, minimap.js, mapview.js, inspector.js) — this is the only module
 * that knows how they fit together.
 */

import * as mapview from "./mapview.js";
import * as track from "./track.js";
import { createMinimap } from "./minimap.js";
import { renderDeviceLegend, renderInspector, renderSummary } from "./inspector.js";
import { initDayNav, initialDate } from "./daynav.js";

const el = (id) => document.getElementById(id);

const state = {
  date: initialDate(),
  showRaw: false,
  day: null,
};

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
}

async function loadPoints(day) {
  const params = new URLSearchParams({ from: day.start_ts, to: day.end_ts, limit: "50000" });
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

async function renameStay(item) {
  await mapview.renameStay(item, load);
}

const minimap = createMinimap(el("minimap-places"), { nowEl: el("minimap-now") });

track.initTrack(
  {
    trackScroll: el("track-scroll"),
    trackInner: el("track-inner"),
    headerEl: el("lane-headers"),
    bodyEl: el("lane-body"),
    rulerEl: el("ruler"),
    placeLayerEl: el("place-layer"),
    sleepLayerEl: el("sleep-layer"),
    nowLineEl: el("now-line"),
    nowChipEl: el("now-chip"),
    scrubLineEl: el("scrub-line"),
    scrubChipEl: el("scrub-chip"),
  },
  {
    minimap,
    onSelect: (selection) => {
      if (state.day) renderInspector(el("inspector"), selection, state.day, { onRename: renameStay });
    },
  }
);

/* -- loading ---------------------------------------------------------------- */

async function load() {
  const day = await getJSON(`/api/v1/days/${state.date}`);
  state.day = day;

  mapview.clearDayLayers();
  renderSummary(el("summary"), day);
  renderDeviceLegend(el("device-legend"), day);
  el("empty").hidden = day.items.length > 0;

  track.renderDay(day); // also resets selection, which repaints the inspector overview
  minimap.draw(day);
  mapview.renderStays(day, { onRename: renameStay });

  const points = await loadPoints(day);
  track.setPoints(points);
  const trusted = points.filter((p) => !p.anomaly);
  if (trusted.length > 1) mapview.renderTrack(trusted);
  if (state.showRaw && points.length) mapview.renderRaw(points);

  const bounds = [];
  for (const p of trusted) bounds.push([p.lat, p.lon]);
  for (const item of day.items.filter((i) => i.type === "stay")) bounds.push([item.lat, item.lon]);
  if (bounds.length) mapview.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
}

/* -- wiring ------------------------------------------------------------------ */

initDayNav({
  date: state.date,
  onDateChange: (date) => {
    state.date = date;
    load();
  },
  // Repaints from the already-loaded day — a theme swap never needs a refetch.
  onThemeChange: () => {
    if (!state.day) return;
    track.renderDay(state.day);
    minimap.draw(state.day);
    renderSummary(el("summary"), state.day);
    renderDeviceLegend(el("device-legend"), state.day);
  },
});

el("show-raw").addEventListener("change", (e) => {
  state.showRaw = e.target.checked;
  load();
});

document.querySelectorAll("#zoom-presets button").forEach((btn) => {
  btn.addEventListener("click", () => track.applyPreset(btn.dataset.zoom));
});

const drawBtn = el("draw-area");
drawBtn.addEventListener("click", () => {
  if (mapview.isDrawing()) {
    mapview.stopDrawing();
    return;
  }
  mapview.startDrawing({
    onButtonChange: (active) => {
      drawBtn.textContent = active ? "Cancel" : "+ Area";
      drawBtn.classList.toggle("active", active);
    },
    onCreated: load,
  });
});

// Day navigation and the theme picker live in daynav.js, shared with the
// breakdown page; zoom is the timeline's own.
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (e.key === "+" || e.key === "=") return void track.zoomStep(1);
  if (e.key === "-" || e.key === "_") return void track.zoomStep(-1);
});

mapview.loadAreas();
load().catch((err) => {
  el("empty").hidden = false;
  el("empty").textContent = `Could not load: ${err.message}`;
});
