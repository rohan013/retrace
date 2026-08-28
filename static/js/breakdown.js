/* The day breakdown: a two-ring donut of where the day went.
 *
 * The inner ring is where you were, the outer ring subdivides each place by
 * what you were doing there. Both rings total the whole day, because the
 * server has already resolved the overlaps between streams (see
 * app/breakdown.py) -- this module only draws what it is handed.
 *
 * Drawn as inline SVG rather than canvas: the place colours come back as
 * `oklch(...)` strings and one of them is the literal `var(--muted)`, neither
 * of which a canvas context can take.
 */

import {
  LANE_FALLBACK,
  LANE_META,
  PLACE_COLOR,
  duration,
  escapeHTML,
  placeHue,
  setSubjectColors,
  subjectColor,
} from "./format.js";
import { initDayNav, initialDate } from "./daynav.js";

const el = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";

const CENTRE = 150;
const INNER = { r0: 60, r1: 99 };
const OUTER = { r0: 103, r1: 142 };

// A 2px gap in surface colour between neighbouring arcs, expressed as the angle
// that subtends 2px at the ring's own mid-radius -- so both rings show the same
// visual gap despite their different radii.
const GAP_PX = 2;

// Below this share a wedge is too thin to hold a readable label, so it gets one
// in the list instead of on the chart. Labelling every wedge is what turns a
// donut into a wall of leader lines.
const LABEL_MIN_SHARE = 0.09;

// Past six the wedges stop being distinguishable, so the tail becomes one slice
// rather than a scatter of slivers. Activities need no equivalent: that set is a
// fixed six by construction.
const MAX_PLACES = 6;
const OTHER_PLACES = "Other places";

/* -- identity ---------------------------------------------------------------
 * Activity colours are a fixed table, so a slice keeps its colour whatever the
 * day holds and wherever it ranks. They are read through functions because
 * `setTheme` swaps the palette in place.
 */

// `--muted` and LANE_FALLBACK.color are the same grey, so the two absence
// labels have to differ some other way: doing something untracked is a real
// grey, while no signal at all takes the near-background tone a hole should
// have. They sit next to each other constantly, and telling them apart is the
// whole reason they are separate slices.
const ABSENT = "var(--line)";

const ACTIVITY_COLOR = {
  Sleep: () => LANE_META.sleep.color,
  Reddit: () => subjectColor("reddit"),
  YouTube: () => subjectColor("youtube.com"),
  Other: () => LANE_FALLBACK.color,
  Untracked: () => ABSENT,
};

const activityColor = (label) => (ACTIVITY_COLOR[label] || (() => LANE_FALLBACK.color))();

// "Moving" borrows the colour trips already wear on the timeline, and the two
// labels that mean "no record" wear the muted ink everything else uses for
// absence. Everywhere you actually were hashes to its own hue, so Home and Work
// look the same here as they do on the track.
function placeColor(place) {
  if (place.label === "Moving") return PLACE_COLOR;
  if (place.label === "No location") return ABSENT;
  if (place.label === OTHER_PLACES) return LANE_FALLBACK.color;
  return placeHue({
    name: place.label,
    place_id: place.place_id,
    area_id: place.area_id,
    lat: place.lat,
    lon: place.lon,
  });
}

/** `duration` floors at whole minutes, which reads as "0m" for a sliver that is
 *  really there. A breakdown that accounts for every second should not print
 *  zero for one of them. */
const span = (seconds) => (seconds > 0 && seconds < 60 ? "<1m" : duration(seconds));

const percent = (share) => `${(share * 100).toFixed(share < 0.1 ? 1 : 0)}%`;

/* -- geometry ---------------------------------------------------------------- */

function point(radius, angle) {
  return [CENTRE + radius * Math.cos(angle), CENTRE + radius * Math.sin(angle)];
}

/** An annular sector. A slice covering the whole turn has no start and end to
 *  draw between, so it is a ring rather than a path. */
function arc(ring, from, to) {
  const node = document.createElementNS(SVG_NS, to - from >= Math.PI * 2 - 1e-6 ? "circle" : "path");
  if (node.tagName === "circle") {
    node.setAttribute("cx", CENTRE);
    node.setAttribute("cy", CENTRE);
    node.setAttribute("r", (ring.r0 + ring.r1) / 2);
    node.setAttribute("fill", "none");
    // Inline style, not a presentation attribute: `.arc` in the stylesheet sets
    // the hairline that separates neighbouring wedges, and a CSS rule outranks
    // an attribute -- which would draw this ring as a 1px line.
    node.style.strokeWidth = `${ring.r1 - ring.r0}px`;
    return node;
  }
  const large = to - from > Math.PI ? 1 : 0;
  const [ax, ay] = point(ring.r1, from);
  const [bx, by] = point(ring.r1, to);
  const [cx, cy] = point(ring.r0, to);
  const [dx, dy] = point(ring.r0, from);
  node.setAttribute(
    "d",
    `M${ax},${ay} A${ring.r1},${ring.r1} 0 ${large} 1 ${bx},${by} ` +
      `L${cx},${cy} A${ring.r0},${ring.r0} 0 ${large} 0 ${dx},${dy} Z`
  );
  return node;
}

function paint(node, color) {
  // Set through the style property, not the `fill` attribute: `var(--muted)`
  // resolves as a CSS value and would be ignored as a presentation attribute.
  if (node.tagName === "circle") node.style.stroke = color;
  else node.style.fill = color;
}

/* -- drawing ----------------------------------------------------------------- */

/** Top places by time, with the tail rolled into one slice that keeps its own
 *  activities so the outer ring still accounts for every second. */
function topPlaces(places) {
  if (places.length <= MAX_PLACES) return places;
  const head = places.slice(0, MAX_PLACES - 1);
  const tail = places.slice(MAX_PLACES - 1);
  const merged = new Map();
  for (const place of tail) {
    for (const activity of place.activities) {
      merged.set(activity.label, (merged.get(activity.label) || 0) + activity.seconds);
    }
  }
  const seconds = tail.reduce((sum, p) => sum + p.seconds, 0);
  return [
    ...head,
    {
      label: OTHER_PLACES,
      seconds,
      share: tail.reduce((sum, p) => sum + p.share, 0),
      names: tail.map((p) => p.label),
      activities: [...merged.entries()]
        .map(([label, secs]) => ({ label, seconds: secs }))
        .sort((a, b) => b.seconds - a.seconds),
    },
  ];
}

function renderDonut(svg, breakdown, onHover) {
  svg.innerHTML = "";
  const total = breakdown.total_s;
  if (!total) return;

  const places = topPlaces(breakdown.places);
  let angle = -Math.PI / 2; // start at twelve o'clock, sweep clockwise

  for (const place of places) {
    const sweep = (place.seconds / total) * Math.PI * 2;
    const key = place.label;

    // A gap separates neighbours. A slice with no neighbour needs none, and
    // leaving one puts a seam across an otherwise unbroken ring.
    const innerGap = places.length > 1 ? Math.min(GAP_PX / ((INNER.r0 + INNER.r1) / 2), sweep / 4) : 0;
    const inner = arc(INNER, angle, angle + sweep - innerGap);
    paint(inner, placeColor(place));
    inner.dataset.place = key;
    inner.classList.add("arc", "arc-place");
    inner.append(title(`${place.label} · ${span(place.seconds)} · ${percent(place.share)}`));
    svg.appendChild(inner);

    let sub = angle;
    for (const activity of place.activities) {
      const subSweep = (activity.seconds / total) * Math.PI * 2;
      if (subSweep <= 0) continue;
      const gap =
        place.activities.length > 1 ? Math.min(GAP_PX / ((OUTER.r0 + OUTER.r1) / 2), subSweep / 4) : 0;
      const outer = arc(OUTER, sub, sub + subSweep - gap);
      paint(outer, activityColor(activity.label));
      outer.dataset.place = key;
      outer.dataset.activity = activity.label;
      outer.classList.add("arc", "arc-activity");
      outer.append(title(`${place.label} — ${activity.label} · ${span(activity.seconds)}`));
      svg.appendChild(outer);
      sub += subSweep;
    }

    if (place.share >= LABEL_MIN_SHARE) {
      svg.appendChild(wedgeLabel(place, angle + sweep / 2));
    }
    angle += sweep;
  }

  for (const node of svg.querySelectorAll(".arc")) {
    node.addEventListener("mouseenter", () => onHover(node.dataset.place));
    node.addEventListener("mouseleave", () => onHover(null));
  }
}

function title(text) {
  const node = document.createElementNS(SVG_NS, "title");
  node.textContent = text;
  return node;
}

function wedgeLabel(place, angle) {
  const group = document.createElementNS(SVG_NS, "text");
  const [x, y] = point((INNER.r0 + INNER.r1) / 2, angle);
  group.setAttribute("x", x);
  group.setAttribute("y", y);
  group.setAttribute("class", "arc-label");
  group.setAttribute("text-anchor", "middle");
  group.setAttribute("dominant-baseline", "middle");

  const name = document.createElementNS(SVG_NS, "tspan");
  name.setAttribute("x", x);
  name.setAttribute("dy", "-0.5em");
  name.textContent = place.label;
  const value = document.createElementNS(SVG_NS, "tspan");
  value.setAttribute("x", x);
  value.setAttribute("dy", "1.15em");
  value.setAttribute("class", "arc-label-value");
  value.textContent = span(place.seconds);

  group.append(name, value);
  return group;
}

function renderCentre(node, breakdown, day, hovered) {
  const place = hovered && breakdown.places.find((p) => p.label === hovered);
  if (place) {
    node.innerHTML =
      `<div class="centre-value">${span(place.seconds)}</div>` +
      `<div class="centre-label">${escapeHTML(place.label)}</div>` +
      `<div class="centre-sub">${percent(place.share)} of the day</div>`;
    return;
  }
  const recorded = breakdown.activities
    .filter((a) => a.label !== "Untracked")
    .reduce((sum, a) => sum + a.seconds, 0);
  node.innerHTML =
    `<div class="centre-value">${span(recorded)}</div>` +
    `<div class="centre-label">recorded</div>` +
    `<div class="centre-sub">of ${span(breakdown.total_s)}</div>`;
}

function renderList(container, breakdown, onHover) {
  const places = topPlaces(breakdown.places);
  const longest = places.length ? places[0].seconds : 1;

  container.innerHTML = places
    .map((place) => {
      const color = placeColor(place);
      const rolled = place.names ? ` <span class="bd-note">${escapeHTML(place.names.join(", "))}</span>` : "";
      const rows = place.activities
        .map(
          (activity) => `
          <div class="insp-row bd-activity">
            <span class="insp-swatch" style="background:${activityColor(activity.label)}"></span>
            <span class="insp-label">${escapeHTML(activity.label)}</span>
            <span class="insp-value">${span(activity.seconds)}</span>
          </div>`
        )
        .join("");
      return `
        <div class="bd-place" data-place="${escapeHTML(place.label)}">
          <div class="insp-row bd-head">
            <span class="insp-swatch" style="background:${color}"></span>
            <span class="insp-label">${escapeHTML(place.label)}${rolled}</span>
            <span class="insp-bar"><span style="width:${Math.max(3, (place.seconds / longest) * 100)}%;background:${color}"></span></span>
            <span class="insp-value">${span(place.seconds)} &middot; ${percent(place.share)}</span>
          </div>
          ${rows}
        </div>`;
    })
    .join("");

  for (const node of container.querySelectorAll(".bd-place")) {
    node.addEventListener("mouseenter", () => onHover(node.dataset.place));
    node.addEventListener("mouseleave", () => onHover(null));
  }
}

/* -- page -------------------------------------------------------------------- */

const state = { date: initialDate(), day: null, hovered: null };

function setHover(place) {
  state.hovered = place;
  const svg = el("donut");
  svg.classList.toggle("hovering", place != null);
  for (const node of svg.querySelectorAll(".arc")) {
    node.classList.toggle("dim", place != null && node.dataset.place !== place);
  }
  for (const node of el("breakdown-list").querySelectorAll(".bd-place")) {
    node.classList.toggle("active", node.dataset.place === place);
  }
  if (state.day) renderCentre(el("donut-centre"), state.day.breakdown, state.day, place);
}

function paintDay() {
  const day = state.day;
  renderDonut(el("donut"), day.breakdown, setHover);
  renderCentre(el("donut-centre"), day.breakdown, day, null);
  renderList(el("breakdown-list"), day.breakdown, setHover);
  el("donut-caption").textContent = day.items.length
    ? `Inner ring: where. Outer ring: what, within each place. Times are ${day.tz}.`
    : "Nothing was recorded on this day.";
}

// Same as the timeline: colours the user has set, read once before first paint.
// Falling back to hashed colours is not worth failing the page over.
async function loadPreferences() {
  try {
    const response = await fetch("/api/v1/preferences");
    if (!response.ok) throw new Error(String(response.status));
    setSubjectColors((await response.json()).subject_colors);
  } catch (err) {
    console.warn("preferences unavailable, using hashed colours", err);
  }
}

async function load() {
  const response = await fetch(`/api/v1/days/${state.date}`);
  if (!response.ok) throw new Error(`${response.status} loading ${state.date}`);
  state.day = await response.json();
  paintDay();
}

initDayNav({
  date: state.date,
  onDateChange: (date) => {
    state.date = date;
    load().catch(showError);
  },
  onThemeChange: () => state.day && paintDay(),
});

function showError(error) {
  el("breakdown-list").innerHTML = `<p class="insp-empty">Could not load: ${escapeHTML(error.message)}</p>`;
}

loadPreferences().then(load).catch(showError);
