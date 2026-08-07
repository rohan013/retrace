/* The right column's stat tiles, device legend, and inspector panel — the
 * place blocks no longer have to carry three lines of text into, because
 * this panel can hold as much detail as a selection needs.
 */

import { LANE_ORDER, PLACE_COLOR, clockTime, distance, duration, escapeHTML, laneMeta, laneTitle, placeHue, subjectColor } from "./format.js";

export function renderSummary(container, day) {
  const s = day.summary;
  const stats = [
    ["Distance", distance(s.distance_m)],
    ["Moving", duration(s.time_moving_s)],
    ["Stays", String(s.stay_count)],
    ["Stationary", duration(s.time_stationary_s)],
  ];
  container.innerHTML = stats
    .map(
      ([label, value]) =>
        `<div class="stat"><div class="value">${value}</div><div class="label">${label}</div></div>`
    )
    .join("");
}

function truncateDevice(device) {
  return device.length > 14 ? `${device.slice(0, 11)}…` : device;
}

export function renderDeviceLegend(container, day) {
  const counts = new Map();
  for (const item of day.items) {
    if (!item.device) continue;
    counts.set(item.device, (counts.get(item.device) || 0) + 1);
  }
  if (!counts.size) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(
      ([device, n]) =>
        `<span class="device-chip" title="${escapeHTML(device)}">${escapeHTML(truncateDevice(device))} <span class="count">${n}</span></span>`
    )
    .join("");
}

/* -- overview: no selection, "where the day went" -------------------------- */

function topBy(items, keyFn, n) {
  const totals = new Map();
  for (const item of items) {
    const key = keyFn(item);
    if (key == null) continue;
    const dur = Math.max(1, (item.visible_end_ts ?? item.visible_start_ts) - item.visible_start_ts);
    totals.set(key, (totals.get(key) || 0) + dur);
  }
  return [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, n);
}

// colorFor maps a row's label to its colour, so the overview uses exactly the
// same per-subject colours the track does rather than one flat colour per kind.
function rankedSection(title, colorFor, rows, maxSeconds) {
  const bars = rows
    .map(([label, secs]) => {
      const color = colorFor(label);
      return `
      <div class="insp-row">
        <span class="insp-swatch" style="background:${color}"></span>
        <span class="insp-label">${escapeHTML(label)}</span>
        <span class="insp-bar"><span style="width:${Math.max(4, (secs / maxSeconds) * 100)}%;background:${color}"></span></span>
        <span class="insp-value">${duration(secs)}</span>
      </div>`;
    })
    .join("");
  return `<div class="insp-section"><div class="insp-subtitle">${title}</div>${bars}</div>`;
}

function renderOverviewHTML(day) {
  const stays = day.items.filter((i) => i.type === "stay");
  const places = topBy(stays, (s) => s.name || "Unnamed place", 4);
  const hueByPlace = new Map(stays.map((s) => [s.name || "Unnamed place", placeHue(s)]));

  const sections = [];
  if (places.length) {
    sections.push(
      rankedSection("Places", (label) => hueByPlace.get(label) || PLACE_COLOR, places, places[0][1])
    );
  }

  // `session` has no lane and no subject worth ranking; `site` is ranked on its
  // own here even though the track nests it inside the focus lane, because
  // "where did my browser time go" is its own question.
  const eventKinds = [
    ...new Set(day.items.filter((i) => i.type === "event" && i.kind !== "session").map((i) => i.kind)),
  ].sort((a, b) => LANE_ORDER.indexOf(a) - LANE_ORDER.indexOf(b));

  for (const kind of eventKinds) {
    const items = day.items.filter((i) => i.type === "event" && i.kind === kind);
    const top = topBy(items, (i) => i.subject, 5);
    if (top.length) {
      sections.push(rankedSection(laneTitle(kind), (label) => subjectColor(label, kind), top, top[0][1]));
    }
  }

  if (!sections.length) {
    return `<p class="insp-empty">Nothing recorded on this day.</p>`;
  }
  return sections.join("");
}

/* -- selection detail -------------------------------------------------------- */

const CONFIDENCE_LABELS = {
  dwell: "Dwell",
  tightness: "Tightness",
  density: "Density",
  accuracy: "Accuracy",
  place_match: "Place match",
};

function confidenceMeter(breakdown) {
  const rows = Object.entries(breakdown || {})
    .filter(([k]) => CONFIDENCE_LABELS[k])
    .map(
      ([k, v]) => `
      <div class="meter-row">
        <span class="meter-label">${CONFIDENCE_LABELS[k]}</span>
        <span class="meter-track"><span class="meter-fill" style="width:${Math.round(v * 100)}%"></span></span>
      </div>`
    )
    .join("");
  return rows ? `<div class="confidence-meter">${rows}</div>` : "";
}

function stayDetail(item, day) {
  const spansDays = item.visible_duration_s !== item.duration_s;
  const hue = placeHue(item);
  return `
    <div class="insp-header" style="--accent:${hue}">
      <div class="insp-name">${item.name ? escapeHTML(item.name) : '<span class="unnamed">Unnamed place</span>'}</div>
      <div class="insp-span">${clockTime(item.visible_start_ts, day.tz)}–${clockTime(item.visible_end_ts, day.tz)} &middot; ${duration(item.visible_duration_s)}</div>
    </div>
    ${
      spansDays
        ? `<div class="insp-note">${duration(item.duration_s)} in total${item.continuation_of ? `, from ${item.continuation_of}` : ""}</div>`
        : ""
    }
    <div class="insp-stats">
      <div>${item.point_count} fixes</div>
      <div>${Math.round(item.radius_m)} m radius</div>
      ${item.had_gap ? '<span class="badge gap">gap</span>' : ""}
      ${item.confidence < 40 ? '<span class="badge low">low confidence</span>' : ""}
    </div>
    <div class="insp-subtitle">Confidence ${item.confidence}</div>
    ${confidenceMeter(item.confidence_breakdown)}
    ${item.note ? `<div class="insp-body-note">${escapeHTML(item.note)}</div>` : ""}
    <button class="insp-rename" type="button">Rename</button>
  `;
}

function tripDetail(item, day) {
  return `
    <div class="insp-header" style="--accent:${PLACE_COLOR}">
      <div class="insp-name">Moving</div>
      <div class="insp-span">${clockTime(item.visible_start_ts, day.tz)}–${clockTime(item.visible_end_ts, day.tz)} &middot; ${duration(item.visible_duration_s)}</div>
    </div>
    <div class="insp-stats">
      <div>${distance(item.distance_m)}</div>
      ${item.avg_speed ? `<div>avg ${Math.round(item.avg_speed * 3.6)} km/h</div>` : ""}
      ${item.max_speed ? `<div>peak ${Math.round(item.max_speed * 3.6)} km/h</div>` : ""}
    </div>
  `;
}

function eventDetail(item, day) {
  const meta = laneMeta(item.kind);
  const isRange = item.shape === "range";
  return `
    <div class="insp-header" style="--accent:${subjectColor(item.subject, item.kind)}">
      <div class="insp-name">${meta.icon} ${escapeHTML(meta.label(item))}</div>
      <div class="insp-span">${clockTime(item.visible_start_ts, day.tz)}${
        isRange ? `–${clockTime(item.visible_end_ts, day.tz)} &middot; ${duration(item.visible_duration_s)}` : ""
      }</div>
    </div>
    <div class="insp-stats">
      ${item.device ? `<div>${escapeHTML(item.device)}</div>` : ""}
      ${item.ongoing ? '<span class="badge">ongoing</span>' : ""}
      ${item.flagged ? '<span class="badge low">unpaired</span>' : ""}
    </div>
  `;
}

function clusterDetail(block, day) {
  const first = block.items[0];
  const isPlace = first.type === "stay" || first.type === "trip";
  const colorFor = (label) => (isPlace ? PLACE_COLOR : subjectColor(label, first.kind));
  const starts = block.items.map((i) => i.visible_start_ts);
  const ends = block.items.map((i) => i.visible_end_ts ?? i.visible_start_ts);
  const spanStart = Math.min(...starts);
  const spanEnd = Math.max(...ends);
  const rows = block.histogram
    .slice(0, 12)
    .map(
      (h) => `
      <div class="insp-row">
        <span class="insp-swatch" style="background:${colorFor(h.key)}"></span>
        <span class="insp-label">${escapeHTML(h.key)}</span>
        <span class="insp-value">${duration(h.weight)} &middot; ${h.count}&times;</span>
      </div>`
    )
    .join("");
  return `
    <div class="insp-header" style="--accent:${colorFor(block.histogram[0].key)}">
      <div class="insp-name">${block.items.length} items</div>
      <div class="insp-span">${clockTime(spanStart, day.tz)}–${clockTime(spanEnd, day.tz)} &middot; ${duration(spanEnd - spanStart)}</div>
    </div>
    <div class="insp-subtitle">Breakdown</div>
    ${rows}
  `;
}

function renderSelectionHTML(selection, day) {
  if (selection.kind === "cluster") return clusterDetail(selection, day);
  const item = selection.item;
  if (item.type === "stay") return stayDetail(item, day);
  if (item.type === "trip") return tripDetail(item, day);
  return eventDetail(item, day);
}

/** selection is either null (show the overview) or a block object from
 * layout.js — {kind:"single", item} or {kind:"cluster", items, histogram}. */
export function renderInspector(container, selection, day, { onRename } = {}) {
  container.innerHTML = selection ? renderSelectionHTML(selection, day) : renderOverviewHTML(day);
  if (selection?.kind === "single" && selection.item.type === "stay") {
    container.querySelector(".insp-rename")?.addEventListener("click", () => onRename?.(selection.item));
  }
}
