/* The scrollable, zoomable day track: ruler, lane headers, and blocks laid
 * out by layout.js. Vertical time axis — scrolling moves through the day,
 * ⌘/Ctrl+wheel (or the preset buttons) changes pxPerMinute.
 */

import {
  LANE_ORDER,
  clockTime,
  distance,
  duration,
  escapeHTML,
  hexToRgb,
  isSystemSubject,
  laneMeta,
  laneTitle,
  placeHue,
  subjectColor,
  tickInterval,
  tickLabel,
} from "./format.js";
import { FULL_LABEL_MIN_PX, LABEL_MIN_PX, densityBuckets, layoutLane, yFor as layoutYFor } from "./layout.js";
import * as mapview from "./mapview.js";

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// 6000 px/min is 100px per second, so a one-second app switch renders as a
// fully-labelled block. Going finer buys nothing real: events.ts is an INTEGER
// number of seconds, so one second is the floor the data can actually resolve.
const ZOOM_MAX = 6000;
const COLLAPSED_STRIP_ROWS = 2000; // fixed backing resolution; CSS stretches it to content height
const COLLAPSE_KEY = "retrace.collapsedLanes";

const PRESET_MINUTES = { "1h": 60, "10m": 10, "1m": 1, "10s": 1 / 6 };

// A site event's timestamps always fall inside the focus block it happened
// under (the agent only polls tabs while a tracked browser is frontmost), so
// site blocks render nested inside their enclosing focus block rather than in
// a separate lane. Grouping here just keeps a site item and an app item from
// ever merging into the same cluster — see FOCUS_SPANS below for how each
// group is actually positioned.
const FOCUS_GROUPS = [
  { key: "app", of: (i) => i.kind === "focus" },
  { key: "site", of: (i) => i.kind === "site" },
];

// app fills the whole lane; site insets from both edges so the parent
// block's colour still frames it, reading as a box nested inside a box.
const FOCUS_SPANS = {
  app: { offset: 0, width: 1 },
  site: { offset: 0.04, width: 0.92 },
};

// How often the now-line steps. At the deepest zoom a second is 100px, so a
// slower tick would visibly lag; a single style write per second is nothing.
const NOW_TICK_MS = 1000;

let trackScroll, trackInner, headerEl, bodyEl, rulerEl, scrubLineEl, scrubChipEl, placeLayerEl;
let nowLineEl, nowChipEl;
let nowTimer = null;
let minimap = null;
let onSelect = () => {};

let day = null;
let points = [];
let pxPerMinute = 1;
let zoomMin = 0.6;
let lanes = [];
let selection = null;
let contentHeight = 0;
let clusterSeq = 0;

const collapsedLanes = loadCollapsed();

function loadCollapsed() {
  try {
    return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]"));
  } catch {
    return new Set();
  }
}
function saveCollapsed() {
  localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...collapsedLanes]));
}

/* -- init --------------------------------------------------------------- */

export function initTrack(refs, opts) {
  ({ trackScroll, trackInner, headerEl, bodyEl, rulerEl, scrubLineEl, scrubChipEl, placeLayerEl,
     nowLineEl, nowChipEl } = refs);
  minimap = opts.minimap;
  onSelect = opts.onSelect || (() => {});

  if (nowTimer) clearInterval(nowTimer);
  nowTimer = setInterval(updateNowLine, NOW_TICK_MS);

  trackScroll.addEventListener("wheel", onWheel, { passive: false });
  trackScroll.addEventListener("scroll", onScroll, { passive: true });
  bodyEl.addEventListener("mousemove", onTrackMouseMove);
  bodyEl.addEventListener("mouseleave", onTrackMouseLeave);
  bodyEl.addEventListener("click", (e) => {
    if (e.target === bodyEl || e.target === rulerEl) setSelection(null);
  });
  window.addEventListener("resize", () => {
    zoomMin = zoomMinFor(day);
    if (day) render();
  });
}

/* -- public API ----------------------------------------------------------- */

export function renderDay(newDay) {
  day = newDay;
  points = [];
  selection = null;
  onSelect(null);
  lanes = buildLaneList(day);
  zoomMin = zoomMinFor(day);
  pxPerMinute = initialZoom(day);
  render();
  const first = day.summary.first_ts ?? day.start_ts;
  trackScroll.scrollTop = Math.max(0, layoutYFor(first, day, pxPerMinute) - 40);
  onScroll();
}

export function setPoints(pts) {
  points = [...pts].sort((a, b) => a.ts - b.ts);
}

export function applyPreset(name) {
  if (!day) return;
  if (name === "day") {
    setZoom(zoomMin, { anchorTs: day.start_ts, anchorFrac: 0 });
    return;
  }
  const minutes = PRESET_MINUTES[name];
  if (!minutes) return;
  setZoom(viewportHeight() / minutes, anchorTimestamp());
}

export function zoomStep(dir) {
  if (!day) return;
  setZoom(pxPerMinute * (dir > 0 ? 1.4 : 1 / 1.4), { anchorFrac: 0.5 });
}

/* -- zoom / pan ------------------------------------------------------------
 * The lane header row is sticky, not overlaid, so it permanently occupies
 * part of trackScroll's viewport once scrolled past. Every calculation that
 * treats "how much of the body is visible" as a height uses this rather than
 * trackScroll.clientHeight directly, so cursor-anchored zoom lands under the
 * cursor rather than off by the header's height.
 */

function viewportHeight() {
  return Math.max(1, trackScroll.clientHeight - (headerEl?.offsetHeight || 0));
}

function zoomMinFor(d) {
  const totalMinutes = (d.end_ts - d.start_ts) / 60;
  return Math.max(0.05, viewportHeight() / totalMinutes);
}

function initialZoom(d) {
  const first = d.summary.first_ts ?? d.start_ts;
  const last = d.summary.last_ts ?? d.end_ts;
  const spanMinutes = Math.max(20, (last - first) / 60);
  const target = (viewportHeight() * 0.85) / spanMinutes;
  return clamp(target, zoomMin, ZOOM_MAX);
}

function centerTimestamp(frac) {
  const y = trackScroll.scrollTop + frac * viewportHeight();
  return day.start_ts + (y / pxPerMinute) * 60;
}

function setZoom(next, { anchorTs, anchorFrac = 0.5 } = {}) {
  if (!day) return;
  const clamped = clamp(next, zoomMin, ZOOM_MAX);
  const ts = anchorTs ?? centerTimestamp(anchorFrac);
  pxPerMinute = clamped;
  render();
  const newY = layoutYFor(ts, day, pxPerMinute);
  const maxScroll = Math.max(0, contentHeight - viewportHeight());
  trackScroll.scrollTop = clamp(newY - anchorFrac * viewportHeight(), 0, maxScroll);
  onScroll();
}

function onWheel(e) {
  if (!(e.ctrlKey || e.metaKey) || !day) return;
  e.preventDefault();
  const rect = trackScroll.getBoundingClientRect();
  const headerH = headerEl?.offsetHeight || 0;
  const frac = clamp((e.clientY - rect.top - headerH) / viewportHeight(), 0, 1);
  const anchorTs = centerTimestamp(frac);
  const factor = Math.exp(-e.deltaY * 0.0025);
  setZoom(pxPerMinute * factor, { anchorTs, anchorFrac: frac });
}

let rulerRaf = null;

// Scroll fires far more often than a render() does, so the ruler's own redraw
// is rAF-throttled here rather than run inline — mirrors the pattern already
// used for the scrub cursor below.
function scheduleRulerUpdate() {
  if (rulerRaf) return;
  rulerRaf = requestAnimationFrame(() => {
    rulerRaf = null;
    renderRuler();
  });
}

function onScroll() {
  minimap?.setViewport(trackScroll.scrollTop, viewportHeight(), contentHeight);
  scheduleRulerUpdate();
}

function itemSpan(block) {
  const items = block.kind === "cluster" ? block.items : [block.item];
  const starts = items.map((i) => i.visible_start_ts);
  const ends = items.map((i) => i.visible_end_ts ?? i.visible_start_ts);
  return { start: Math.min(...starts), end: Math.max(...ends) };
}

// How far a timestamp sits from an item's span — 0 when it falls inside.
function distanceToItem(item, ts) {
  const start = item.visible_start_ts;
  const end = item.visible_end_ts ?? start;
  if (ts >= start && ts <= end) return 0;
  return Math.min(Math.abs(ts - start), Math.abs(ts - end));
}

// Where a preset should zoom to: the current selection's start if there is one,
// otherwise wherever real activity is nearest the viewport's current center.
// Anchoring on the raw center (the old behavior) is what let `10m` land on a
// random empty hour — an empty patch of the day has nothing to zoom in on.
function anchorTimestamp() {
  if (selection) {
    const { start } = itemSpan(selection);
    return { anchorTs: start, anchorFrac: 0.35 };
  }
  if (!day.items.length) return { anchorFrac: 0.5 };
  const center = centerTimestamp(0.5);
  let nearest = day.items[0];
  let bestDist = distanceToItem(nearest, center);
  for (const item of day.items) {
    const d = distanceToItem(item, center);
    if (d < bestDist) {
      bestDist = d;
      nearest = item;
    }
  }
  if (bestDist === 0) return { anchorFrac: 0.5 };
  return { anchorTs: nearest.visible_start_ts, anchorFrac: 0.35 };
}

function zoomToFit(block) {
  const { start, end } = itemSpan(block);
  const minutes = Math.max(0.05, (end - start) / 60);
  const target = (viewportHeight() * 0.6) / minutes;
  setZoom(target, { anchorTs: (start + end) / 2, anchorFrac: 0.5 });
}

/* -- lane structure --------------------------------------------------------- */

// Lanes are event kinds only — stays and trips are the background behind them
// now, not a column of their own. `session` never gets a lane (focus already
// says what was on screen) and `site` never gets one either (it rides inside
// the focus lane), but both stay in day.items for the inspector and minimap.
function buildLaneList(d) {
  const kinds = new Set(d.items.filter((i) => i.type === "event").map((i) => i.kind));
  const ordered = LANE_ORDER.filter((k) => kinds.has(k));
  const extra = [...kinds].filter((k) => !LANE_ORDER.includes(k) && k !== "session" && k !== "site").sort();
  const lanes = [...ordered, ...extra];
  // A day with sites but somehow no focus events still needs somewhere to put
  // them rather than dropping them silently.
  if (kinds.has("site") && !lanes.includes("focus")) lanes.push("focus");
  return lanes;
}

function laneItems(lane) {
  if (lane === "focus") {
    return day.items.filter((i) => i.type === "event" && (i.kind === "focus" || i.kind === "site"));
  }
  return day.items.filter((i) => i.type === "event" && i.kind === lane);
}

const laneGroups = (lane) => (lane === "focus" ? FOCUS_GROUPS : null);

function laneColumnWidth(lane) {
  if (collapsedLanes.has(lane)) return "14px";
  return lane === "focus" ? "2.2fr" : "minmax(110px, 1fr)";
}

/* -- render ------------------------------------------------------------------ */

function render() {
  contentHeight = layoutYFor(day.end_ts, day, pxPerMinute);

  const columns = ["48px", ...lanes.map(laneColumnWidth)].join(" ");
  headerEl.style.gridTemplateColumns = columns;
  bodyEl.style.gridTemplateColumns = columns;

  renderHeaders();
  renderRuler();
  renderPlaceBackground();
  renderLanes();
  updateNowLine();
}

function renderHeaders() {
  headerEl.innerHTML = "";
  headerEl.appendChild(el("div", "corner"));
  for (const lane of lanes) {
    const meta = laneMeta(lane);
    const count = laneItems(lane).length;
    const header = el("div", "lane-header");
    header.style.setProperty("--accent", meta.color);
    const sub = lane === "focus" && laneItems(lane).some((i) => i.kind === "site") ? " + sites" : "";
    header.innerHTML =
      `<span class="dot"></span>` +
      `<span class="name">${meta.icon} ${laneTitle(lane)}${sub}</span>` +
      `<span class="count">${count}</span>`;
    header.classList.toggle("collapsed", collapsedLanes.has(lane));
    header.title = collapsedLanes.has(lane) ? "Click to expand" : "Click to collapse";
    header.addEventListener("click", () => toggleCollapse(lane));
    headerEl.appendChild(header);
  }
}

/* -- place background --------------------------------------------------------
 * Stays and trips wash across every lane rather than occupying a column, so a
 * focus block reads against where you were at the time. Each band keeps its
 * own click target; the name/duration identifying it lives on the minimap
 * rail (minimap.js) rather than floating a label over the lanes.
 */
function renderPlaceBackground() {
  placeLayerEl.innerHTML = "";
  placeLayerEl.style.height = `${contentHeight}px`;

  const items = day.items.filter((i) => i.type === "stay" || i.type === "trip");
  for (const item of items) {
    const top = layoutYFor(item.visible_start_ts, day, pxPerMinute);
    const height = Math.max(2, layoutYFor(item.visible_end_ts, day, pxPerMinute) - top);
    const band = el("div", `place-band ${item.type}`);
    band.style.top = `${top}px`;
    band.style.height = `${height}px`;
    band.style.setProperty("--accent", item.type === "stay" ? placeHue(item) : "var(--muted)");

    const key = `${item.type}-${item.id}`;
    band.dataset.key = key;
    if (selection?.kind === "single" && blockKey(selection) === key) band.classList.add("active");
    band.title = tooltipText(item);

    band.addEventListener("click", () => {
      setSelection({ kind: "single", item });
      if (item.type === "stay") mapview.focusStay(item);
    });
    band.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      zoomToFit({ kind: "single", item });
    });
    band.addEventListener("mouseenter", () => setHover(key, true));
    band.addEventListener("mouseleave", () => setHover(key, false));
    placeLayerEl.appendChild(band);
  }
}

function toggleCollapse(lane) {
  if (collapsedLanes.has(lane)) collapsedLanes.delete(lane);
  else collapsedLanes.add(lane);
  saveCollapsed();
  render();
  onScroll();
}

// Ticks step from hourly down to 10s as pxPerMinute grows (tickInterval), and
// are windowed to roughly the visible scroll range — at the deepest zoom a full
// day is 8,640 ten-second ticks, and almost none of them are ever on screen.
function renderRuler() {
  rulerEl.innerHTML = "";
  rulerEl.style.height = `${contentHeight}px`;
  if (!day) return;

  const interval = tickInterval(pxPerMinute);
  const pxPerTick = (interval / 60) * pxPerMinute;
  const totalTicks = Math.ceil((day.end_ts - day.start_ts) / interval);
  const margin = viewportHeight();
  const visTop = Math.max(0, trackScroll.scrollTop - margin);
  const visBottom = Math.min(contentHeight, trackScroll.scrollTop + viewportHeight() + margin);
  const startIdx = Math.max(0, Math.floor(visTop / pxPerTick));
  const endIdx = Math.min(totalTicks, Math.ceil(visBottom / pxPerTick));

  for (let idx = startIdx; idx <= endIdx; idx++) {
    const tick = el("div", "tick");
    tick.style.top = `${idx * pxPerTick}px`;
    tick.textContent = tickLabel(day.start_ts + idx * interval, day.tz, interval);
    rulerEl.appendChild(tick);
  }
}

function renderLanes() {
  // Clear previous lane columns — everything in bodyEl except the persistent
  // ruler, place background, now line and scrub line.
  const keep = new Set([rulerEl, scrubLineEl, placeLayerEl, nowLineEl]);
  [...bodyEl.children].forEach((child) => {
    if (!keep.has(child)) child.remove();
  });

  for (const lane of lanes) {
    const container = el("div", "lane");
    container.dataset.lane = lane;
    container.style.height = `${contentHeight}px`;
    bodyEl.appendChild(container);

    const items = laneItems(lane);
    if (collapsedLanes.has(lane)) {
      renderCollapsedLane(container, lane, items);
    } else {
      renderExpandedLane(container, lane, items);
    }
  }
  // Re-appended last so the cursors stay above the lane columns.
  bodyEl.appendChild(nowLineEl);
  bodyEl.appendChild(scrubLineEl);
}

function renderCollapsedLane(container, lane, items) {
  const canvas = document.createElement("canvas");
  canvas.width = 1;
  canvas.height = COLLAPSED_STRIP_ROWS;
  canvas.style.width = "100%";
  canvas.style.height = `${contentHeight}px`;
  canvas.style.display = "block";
  container.appendChild(canvas);

  const ctx = canvas.getContext("2d");
  const buckets = densityBuckets(items, day, COLLAPSED_STRIP_ROWS);
  const [r, g, b] = hexToRgb(laneMeta(lane).color);
  const img = ctx.createImageData(1, COLLAPSED_STRIP_ROWS);
  for (let row = 0; row < COLLAPSED_STRIP_ROWS; row++) {
    const a = buckets[row];
    const i = row * 4;
    img.data[i] = r;
    img.data[i + 1] = g;
    img.data[i + 2] = b;
    img.data[i + 3] = Math.round((0.12 + a * 0.75) * 255);
  }
  ctx.putImageData(img, 0, 0);
}

function renderExpandedLane(container, lane, items) {
  const groups = laneGroups(lane);
  const blocks = layoutLane(items, day, pxPerMinute, groups);
  const spans = lane === "focus" ? FOCUS_SPANS : null;

  for (const block of blocks) {
    container.appendChild(renderBlock(block, lane, spans?.[block.group]));
  }
}

/* -- blocks ------------------------------------------------------------------ */

function blockKey(block) {
  if (block.kind === "cluster") {
    if (!block._key) block._key = `cluster-${clusterSeq++}`;
    return block._key;
  }
  const item = block.item;
  if (item.type === "stay" || item.type === "trip") return `${item.type}-${item.id}`;
  return `event-${item.kind}-${item.subject ?? ""}-${item.visible_start_ts}`;
}

// Colour comes from the subject, not the lane — Chrome is Chrome-blue whether
// it shows up on the phone or the Mac.
function blockAccent(item) {
  if (item.type === "stay") return placeHue(item);
  if (item.type === "trip") return "var(--muted)";
  return subjectColor(item.subject, item.kind);
}

function blockClassName(item) {
  if (item.type === "stay") return "block stay";
  if (item.type === "trip") return "block trip";
  const system = isSystemSubject(item.subject) ? " system" : "";
  return `block event ${item.shape}${item.ongoing ? " ongoing" : ""}${item.flagged ? " flagged" : ""}${system}`;
}

function clusterAccent(block) {
  // A cluster of one app keeps that app's colour; a mixed one goes neutral
  // rather than picking a winner the label doesn't claim.
  const subjects = new Set(block.items.map((i) => i.subject));
  return subjects.size === 1 ? subjectColor([...subjects][0], block.items[0].kind) : "var(--muted)";
}

function renderBlock(block, lane, span) {
  const node = document.createElement("div");
  const groupWidth = span ? span.width : 1;
  const groupOffset = span ? span.offset : 0;
  const widthPct = (groupWidth / block.cols) * 100;
  const leftPct = (groupOffset + (block.col / block.cols) * groupWidth) * 100;
  node.style.top = `${block.top}px`;
  node.style.height = `${block.height}px`;
  node.style.left = `calc(${leftPct}% + 2px)`;
  node.style.width = `calc(${widthPct}% - 4px)`;

  const key = blockKey(block);
  node.dataset.key = key;

  if (block.kind === "cluster") {
    node.className = "block cluster event";
    node.style.setProperty("--accent", clusterAccent(block));
    node.title = clusterTooltip(block);
    node.innerHTML = clusterLabelHTML(block);
  } else {
    const item = block.item;
    node.className = blockClassName(item);
    node.style.setProperty("--accent", blockAccent(item));
    node.title = tooltipText(item);
    node.innerHTML = labelHTML(item, block.height);
  }
  // The nested-inside-focus ring only earns its keep once a block is tall
  // enough to actually carry a label — below that, `box-shadow` (never
  // clipped by `overflow: hidden`) would smear across abutting slivers.
  if (block.group === "site" && block.height >= LABEL_MIN_PX) node.classList.add("nested");
  if (selection && blockKey(selection) === key) node.classList.add("active");

  node.addEventListener("mouseenter", () => setHover(key, true));
  node.addEventListener("mouseleave", () => setHover(key, false));
  node.addEventListener("click", (e) => {
    e.stopPropagation();
    setSelection(block);
    if (block.kind === "single" && block.item.type === "stay") mapview.focusStay(block.item);
  });
  node.addEventListener("dblclick", (e) => {
    e.stopPropagation();
    zoomToFit(block);
  });
  return node;
}

function setHover(key, on) {
  bodyEl.querySelectorAll(`[data-key="${CSS.escape(key)}"]`).forEach((n) => n.classList.toggle("hover", on));
  if (key.startsWith("stay-")) mapview.highlightMarker(key, on);
}

function setSelection(block) {
  selection = block;
  bodyEl.querySelectorAll(".block.active").forEach((n) => n.classList.remove("active"));
  if (block) {
    const node = bodyEl.querySelector(`[data-key="${CSS.escape(blockKey(block))}"]`);
    node?.classList.add("active");
  }
  onSelect(block);
}

/* -- labels ------------------------------------------------------------------ */

function blockLabel(item) {
  const when = clockTime(item.visible_start_ts, day.tz);
  const carried = item.continuation_of ? `<span class="badge">from ${item.continuation_of}</span>` : "";

  if (item.type === "stay") {
    const name = item.name ? `<span>${escapeHTML(item.name)}</span>` : `<span class="unnamed">Unnamed place</span>`;
    const gap = item.had_gap ? `<span class="badge gap">gap</span>` : "";
    const low = item.confidence < 40 ? `<span class="badge low">low confidence</span>` : "";
    return `
      <div class="when">${when}</div>
      <div class="title">${name}${carried}</div>
      <div class="detail">${duration(item.visible_duration_s)} &middot; ${item.point_count} fixes &middot; ${Math.round(item.radius_m)} m ${gap}${low}</div>`;
  }
  if (item.type === "trip") {
    return `
      <div class="when">${when}</div>
      <div class="title">Moving${carried}</div>
      <div class="detail">${distance(item.distance_m)} &middot; ${duration(item.visible_duration_s)}${
        item.max_speed ? ` &middot; peak ${Math.round(item.max_speed * 3.6)} km/h` : ""
      }</div>`;
  }
  const meta = laneMeta(item.kind);
  const device = item.device ? `<span class="badge">${escapeHTML(item.device)}</span>` : "";
  return `
    <div class="when">${when}</div>
    <div class="title">${meta.icon} ${escapeHTML(meta.label(item))}${device}</div>
    <div class="detail">${duration(item.visible_duration_s)}${item.ongoing ? " &middot; ongoing" : ""}</div>`;
}

function compactLabel(item) {
  const when = clockTime(item.visible_start_ts, day.tz);
  if (item.type === "stay") {
    const name = item.name ? escapeHTML(item.name) : "Unnamed place";
    return `<div class="line">${when} &middot; ${name} &middot; ${duration(item.visible_duration_s)}</div>`;
  }
  if (item.type === "trip") {
    return `<div class="line">${when} &middot; Moving &middot; ${distance(item.distance_m)}</div>`;
  }
  const meta = laneMeta(item.kind);
  return `<div class="line">${meta.icon} ${escapeHTML(meta.label(item))}</div>`;
}

function labelHTML(item, height) {
  if (height >= FULL_LABEL_MIN_PX) return blockLabel(item);
  if (height >= LABEL_MIN_PX) return compactLabel(item);
  return "";
}

function tooltipText(item) {
  const when = clockTime(item.visible_start_ts, day.tz);
  if (item.type === "stay" || item.type === "trip") {
    const until = clockTime(item.visible_end_ts, day.tz);
    const bits = [`${when}–${until} (${duration(item.visible_duration_s)})`];
    if (item.type === "stay") {
      bits.push(item.name || "Unnamed place");
      bits.push(`${item.point_count} fixes, ${Math.round(item.radius_m)} m radius`);
      bits.push(`confidence ${item.confidence}`);
      if (item.had_gap) bits.push("includes a reporting gap");
    } else {
      bits.push("Moving");
      bits.push(distance(item.distance_m));
      if (item.max_speed) bits.push(`peak ${Math.round(item.max_speed * 3.6)} km/h`);
    }
    if (item.continuation_of) bits.push(`continued from ${item.continuation_of}`);
    return bits.join(" · ");
  }
  const meta = laneMeta(item.kind);
  const bits = [
    item.shape === "range"
      ? `${when}–${clockTime(item.visible_end_ts, day.tz)} (${duration(item.visible_duration_s)})`
      : when,
    meta.label(item),
  ];
  if (item.device) bits.push(item.device);
  if (item.flagged) bits.push("unpaired");
  if (item.ongoing) bits.push("ongoing");
  return bits.join(" · ");
}

function clusterLabelHTML(block) {
  const top = block.histogram[0];
  if (block.height >= FULL_LABEL_MIN_PX) {
    const rows = block.histogram
      .slice(0, 3)
      .map((h) => `<div class="line">${escapeHTML(h.key)} &times;${h.count}</div>`)
      .join("");
    const more = block.histogram.length > 3 ? `<div class="line more">+${block.histogram.length - 3} more</div>` : "";
    return `<div class="cluster-label">${rows}${more}</div>`;
  }
  if (block.height >= LABEL_MIN_PX) {
    const label = block.histogram.length > 1 ? `${escapeHTML(top.key)} +${block.histogram.length - 1}` : escapeHTML(top.key);
    return `<div class="line">${label} &middot; ${block.items.length}&times;</div>`;
  }
  return "";
}

function clusterTooltip(block) {
  const { start, end } = itemSpan(block);
  const when = `${clockTime(start, day.tz)}–${clockTime(end, day.tz)}`;
  const bits = block.histogram.slice(0, 5).map((h) => `${h.key} ×${h.count}`);
  return `${when} · ${block.items.length} items · ${bits.join(", ")}`;
}

/* -- now line -----------------------------------------------------------------
 * Lives in the track's content space, so zooming and scrolling carry it along
 * for free — only the clock moves it. Hidden outright on any day that does not
 * contain the present moment, rather than pinned to an edge, because a "now"
 * marker frozen at the top of last Tuesday would be a lie.
 */

function updateNowLine() {
  if (!day || !nowLineEl) return;

  const now = Date.now() / 1000;
  if (now < day.start_ts || now >= day.end_ts) {
    nowLineEl.hidden = true;
    minimap?.setNow(null);
    return;
  }

  nowLineEl.hidden = false;
  nowLineEl.style.top = `${layoutYFor(now, day, pxPerMinute)}px`;
  nowChipEl.textContent = clockTime(now, day.tz);
  minimap?.setNow(now);
}

/* -- scrub cursor ------------------------------------------------------------ */

let scrubRaf = null;

function onTrackMouseMove(e) {
  if (!day || scrubRaf) return;
  scrubRaf = requestAnimationFrame(() => {
    scrubRaf = null;
    const rect = bodyEl.getBoundingClientRect();
    // rect.top already reflects the current scroll position, so this is the
    // y-coordinate within bodyEl's own content space — no scrollTop to add.
    const y = e.clientY - rect.top;
    const ts = day.start_ts + (y / pxPerMinute) * 60;
    scrubLineEl.style.top = `${y}px`;
    scrubLineEl.hidden = false;
    scrubChipEl.textContent = clockTime(ts, day.tz);
    if (points.length) mapview.scrubTo(points, ts);
  });
}

function onTrackMouseLeave() {
  scrubLineEl.hidden = true;
  mapview.clearScrub();
}

function el(tag, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}
