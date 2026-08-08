/* Pure formatting helpers and shared reference data — no DOM, no state.
 *
 * The lane palette (LANE_META) is the dataviz-validated 8-hue dark
 * categorical set (see palette.md), assigned in a fixed order across the
 * seven event kinds this app actually sees. Lane identity is never carried
 * by color alone: every lane also has a fixed position and a text label,
 * and every block shows its own text — color reinforces, it doesn't
 * replace either.
 *
 * Places and event subjects are open-ended sets, not a fixed enumerable
 * list, so they draw from their own much larger hue wheels (PLACE_HUES /
 * EVENT_HUES below) instead of the 8-slot lane set — see the comment above
 * those for why.
 */

export const todayISO = () => {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 10);
};

export function shiftDate(iso, days) {
  const d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function clockTime(ts, tz, withSeconds = false) {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: withSeconds ? "2-digit" : undefined,
    timeZone: tz || undefined,
  });
}

export function duration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}

export function distance(metres) {
  return metres >= 1000
    ? `${(metres / 1000).toFixed(1)} km`
    : `${Math.round(metres)} m`;
}

export function hexToRgb(hex) {
  const n = parseInt(hex.replace("#", ""), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// Safe in element content *and* in a quoted attribute. The obvious
// implementation — set textContent, read innerHTML back — is only safe in the
// first: HTML serialises text nodes by escaping `& < >` and leaves quotes
// alone, so a value carrying `"` breaks straight out of `title="…"`. Device
// names, event subjects and trigger types all arrive from ingest, so both
// contexts have to hold.
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHTML(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

// The ruler's tick spacing, coarsest-first: whichever interval is small enough
// that its pixel spacing at the current zoom still clears a readable gap. Floors
// at 10s — finer than that and the deepest preset (10s) would show no ticks at
// all — which also happens to match the whole-second resolution events.ts has.
const TICK_LADDER_S = [21600, 10800, 3600, 1800, 900, 300, 60, 30, 10];
const TICK_MIN_PX = 34;

export function tickInterval(pxPerMinute) {
  for (const seconds of [...TICK_LADDER_S].reverse()) {
    if ((seconds / 60) * pxPerMinute >= TICK_MIN_PX) return seconds;
  }
  return TICK_LADDER_S[TICK_LADDER_S.length - 1];
}

export function tickLabel(ts, tz, intervalSeconds) {
  if (intervalSeconds >= 3600) {
    // hourCycle "h23" pins the range to 0-23 regardless of locale, so the am/pm
    // formatting below stays deterministic instead of depending on the browser's
    // own am/pm strings (which don't exist at all in 24-hour locales).
    const fmt = new Intl.DateTimeFormat([], { hour: "numeric", hourCycle: "h23", timeZone: tz || undefined });
    const hour24 = Number(fmt.format(new Date(ts * 1000)));
    const period = hour24 < 12 ? "am" : "pm";
    const display = hour24 % 12 === 0 ? 12 : hour24 % 12;
    return `${display}${period}`;
  }
  return clockTime(ts, tz, intervalSeconds < 60);
}

export function haversine(a, b) {
  const R = 6371008.8;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

/* -- speed ramp (trips / raw track) ---------------------------------------- */

export const SPEED_SATURATION_MPS = 25; // 90 km/h — the top of the colour ramp
export const SPEED_BUCKETS = 8;

// Blue through green and yellow to red as speed rises.
export function speedColour(mps) {
  const hue = 210 - 210 * Math.min((mps || 0) / SPEED_SATURATION_MPS, 1);
  return `hsl(${hue} 80% 48%)`;
}

export function speedBucket(mps) {
  const share = Math.min((mps || 0) / SPEED_SATURATION_MPS, 1);
  return Math.min(SPEED_BUCKETS - 1, Math.floor(share * SPEED_BUCKETS));
}

/* -- lane palette ------------------------------------------------------------
 * The dataviz-validated dark categorical set (palette.md) is the default
 * theme. THEMES holds a handful of alternates picked from the header's theme
 * picker — only the categorical set and the hash-wheel lightness/chroma
 * change between them; SUBJECT_COLORS (brand hues) stay fixed regardless of
 * theme, since those track real brand colours rather than a design choice.
 * Slot order within `categorical` is fixed across every theme: 1 blue/place,
 * 2 orange/app, 3 aqua/site, 4 yellow/carplay, 5 magenta/session, 6 green/wifi,
 * 7 violet/focus, 8 red/geofence.
 */

const THEMES = {
  default: {
    label: "Default",
    categorical: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
    hashLightness: 60,
    hashChroma: 0.14,
  },
  jewel: {
    label: "Jewel Tone",
    categorical: ["#D9A017", "#C81E4D", "#1E5FD9", "#C9A227", "#D6449E", "#10A85E", "#7C3AED", "#8C1C3E"],
    hashLightness: 50,
    hashChroma: 0.19,
  },
  sunset: {
    label: "Sunset",
    categorical: ["#FF5C5C", "#FFC233", "#8B4FD9", "#FFA366", "#C43FA0", "#FF7A33", "#FF4D8D", "#E23744"],
    hashLightness: 66,
    hashChroma: 0.21,
  },
  ocean: {
    label: "Ocean",
    categorical: ["#22C7E0", "#2D6CDF", "#0E9C8C", "#E0C68A", "#E0568C", "#3ED9B0", "#2A4B8D", "#D9534F"],
    hashLightness: 56,
    hashChroma: 0.14,
  },
  neon: {
    label: "Neon",
    categorical: ["#FFEA00", "#00F0FF", "#9D00FF", "#FFB300", "#FF3D00", "#B6FF00", "#FF00C8", "#FF0044"],
    hashLightness: 68,
    hashChroma: 0.28,
  },
  candy: {
    label: "Candy Pop",
    categorical: ["#6EC3FF", "#FFE066", "#B18CFF", "#FFB37B", "#E066FF", "#5EE6A8", "#FF6FB5", "#FF6B6B"],
    hashLightness: 74,
    hashChroma: 0.15,
  },
};

export const THEME_LIST = Object.entries(THEMES).map(([id, t]) => ({ id, label: t.label }));

let CATEGORICAL_DARK = THEMES.default.categorical;

export let PLACE_COLOR = CATEGORICAL_DARK[0]; // blue — stays/trips

// `session` is deliberately absent: focus already says what was on screen, so
// a separate awake/asleep lane was redundant. `site` is absent too — it renders
// as a sub-column inside the focus lane (see SITE_GROUP in track.js), because a
// site event only ever happens while a browser is frontmost.
export const LANE_ORDER = ["focus", "app", "wifi", "carplay", "geofence"];

export const LANE_META = {
  session: { icon: "💻", title: "Awake", label: () => "Session", color: CATEGORICAL_DARK[4] },
  app: { icon: "📱", title: "Phone", label: (i) => i.subject || "App", color: CATEGORICAL_DARK[1] },
  wifi: { icon: "📶", title: "Wi-Fi", label: (i) => i.subject || "Wi-Fi", color: CATEGORICAL_DARK[5] },
  carplay: { icon: "🚗", title: "CarPlay", label: () => "CarPlay", color: CATEGORICAL_DARK[3] },
  geofence: { icon: "📍", title: "Area", label: (i) => i.subject || "Area", color: CATEGORICAL_DARK[7] },
  focus: { icon: "🖥", title: "Screen", label: (i) => i.subject || "App", color: CATEGORICAL_DARK[6] },
  site: { icon: "🌐", title: "Websites", label: (i) => i.subject || "Site", color: CATEGORICAL_DARK[2] },
};
export const LANE_FALLBACK = { icon: "•", title: "Other", label: (i) => i.subject || i.kind, color: "#8b98ad" };

export const laneMeta = (kind) => LANE_META[kind] || LANE_FALLBACK;
export const laneTitle = (kind) => laneMeta(kind).title || kind;

/* -- hash palettes for open-ended identities ----------------------------------
 * Places and event subjects aren't a fixed enumerable set like lane kinds —
 * real use has dozens of distinct places and apps/sites — so hashing them
 * into the 8-slot lane palette produced frequent collisions, and let a
 * place's background wash and an unrelated event block land on the exact
 * same hue. Each gets its own 28-hue OKLCH wheel instead, one offset from
 * the other so the two pools can never produce an identical hue. Lightness
 * and chroma are fixed per wheel (per theme) so hue is the only thing that
 * varies within it — unlike the lane palette, these are not hand-tuned for
 * pairwise CVD separation; that's traded away here for more variety.
 */
const HASH_HUE_COUNT = 28;
let HASH_LIGHTNESS = THEMES.default.hashLightness; // %, OKLCH
let HASH_CHROMA = THEMES.default.hashChroma; // OKLCH

function hueWheel(count, offsetDeg) {
  return Array.from(
    { length: count },
    (_, i) => `oklch(${HASH_LIGHTNESS}% ${HASH_CHROMA} ${Math.round(offsetDeg + (i * 360) / count)})`
  );
}

let PLACE_HUES = hueWheel(HASH_HUE_COUNT, 0);
let EVENT_HUES = hueWheel(HASH_HUE_COUNT, 180 / HASH_HUE_COUNT);

let currentThemeId = "default";
export const currentTheme = () => currentThemeId;

// Swaps the categorical set and re-lights the hash wheels in place, so every
// existing reference (LANE_META, the exported `let` bindings importers hold a
// live binding to) picks up the new colours on the next render — no need to
// re-import anything or thread a theme value through every call site.
export function setTheme(id) {
  const theme = THEMES[id] ? id : "default";
  const t = THEMES[theme];
  currentThemeId = theme;
  CATEGORICAL_DARK = t.categorical;
  PLACE_COLOR = CATEGORICAL_DARK[0];
  LANE_META.session.color = CATEGORICAL_DARK[4];
  LANE_META.app.color = CATEGORICAL_DARK[1];
  LANE_META.wifi.color = CATEGORICAL_DARK[5];
  LANE_META.carplay.color = CATEGORICAL_DARK[3];
  LANE_META.geofence.color = CATEGORICAL_DARK[7];
  LANE_META.focus.color = CATEGORICAL_DARK[6];
  LANE_META.site.color = CATEGORICAL_DARK[2];
  HASH_LIGHTNESS = t.hashLightness;
  HASH_CHROMA = t.hashChroma;
  PLACE_HUES = hueWheel(HASH_HUE_COUNT, 0);
  EVENT_HUES = hueWheel(HASH_HUE_COUNT, 180 / HASH_HUE_COUNT);
}

/* -- per-subject colour ------------------------------------------------------
 * Blocks are coloured by *what* they are (Chrome, YouTube, iTerm2), not by
 * which lane they sit in — a lane of one flat colour told you nothing you
 * couldn't already read from the column header.
 *
 * These are brand hues lifted into the dark lightness band so every one clears
 * 3:1 on --bg (worst measured 5.29:1). They deliberately depart from the strict
 * categorical palette: recognising Chrome-blue or YouTube-red at a glance is
 * worth more here than pairwise ΔE, and it is safe to trade because colour is
 * never the only identity channel — every block carries its own text label and
 * the inspector names the subject in full.
 *
 * That trade has a real cost worth knowing about. Brands cluster in the warm
 * end — YouTube red, Reddit orangered, Cloudflare orange, Claude clay — so
 * those sit closer together than the ΔE 15 a pure categorical set would hold,
 * and the labels are what carry them apart. Hues are still chosen to maximise
 * the *worst* pair within a sub-column, since only same-column blocks are ever
 * adjacent: Reddit is its true orangered rather than a red-orange (worst pair
 * 5.9 -> 7.7 against YouTube), and Gemini takes its blue-violet rather than
 * another Google blue, which clears every site collision outright.
 */

const SUBJECT_COLORS = {
  // macOS apps (focus)
  "google chrome": "#5A9BFF",
  "brave browser": "#F8943F",
  "microsoft edge": "#4FC3E8",
  arc: "#F76D8E",
  safari: "#5A9BFF",
  firefox: "#FF8A3D",
  iterm2: "#2FD16C",
  terminal: "#2FD16C",
  claude: "#E08663",
  code: "#4FA3F7",
  "visual studio code": "#4FA3F7",
  slack: "#C874D9",
  calendar: "#FF5F55",
  mail: "#5A9BFF",
  messages: "#4ADE80",
  notes: "#F5C451",
  spotify: "#40D97A",
  stremio: "#A78BFA",
  figma: "#F76D8E",
  finder: "#5A9BFF",
  // iPhone apps
  chrome: "#5A9BFF",
  reddit: "#E8620C",
  // sites
  "youtube.com": "#FF5252",
  "reddit.com": "#E8620C",
  "google.com": "#8AB4F8",
  "gemini.google.com": "#7C6BF5",
  "cloudflare.com": "#F8943F",
  "fidelity.com": "#5CC46A",
  "whoop.com": "#35E8AC",
  "claude.ai": "#E08663",
  "github.com": "#A9B4C4",
  "x.com": "#C6CEDA",
  "news.ycombinator.com": "#FF8A3D",
};

// Frameworks and daemons that take focus without you choosing them. Muted so a
// real app never has to compete with `coreautha` for attention.
const SYSTEM_SUBJECTS = new Set([
  "loginwindow",
  "coreautha",
  "usernotificationcenter",
  "windowserver",
  "screensaverengine",
  "dock",
  "systemuiserver",
  "controlcenter",
  "notificationcenter",
]);

export const SYSTEM_COLOR = "#7A8798";

const normalizeSubject = (s) => String(s ?? "").trim().toLowerCase().replace(/^www\./, "");

// "gemini.google.com" -> ["gemini.google.com", "google.com"], so a specific
// entry wins but every *.whoop.com still lands on the Whoop colour.
function domainKeys(s) {
  const parts = s.split(".");
  const keys = [s];
  for (let i = 1; i <= parts.length - 2; i++) keys.push(parts.slice(i).join("."));
  return keys;
}

export function isSystemSubject(subject) {
  return SYSTEM_SUBJECTS.has(normalizeSubject(subject));
}

export function subjectColor(subject, fallbackSeed = "") {
  const key = normalizeSubject(subject);
  if (!key) return LANE_FALLBACK.color;
  if (SYSTEM_SUBJECTS.has(key)) return SYSTEM_COLOR;
  for (const candidate of key.includes(".") ? domainKeys(key) : [key]) {
    if (SUBJECT_COLORS[candidate]) return SUBJECT_COLORS[candidate];
  }
  return EVENT_HUES[hashString(key + fallbackSeed) % EVENT_HUES.length];
}

/* -- stable per-place colour -------------------------------------------------
 * Hashed from identity (place/area id), falling back to rounded coordinates
 * for an unnamed-but-located stay, and a neutral grey when nothing to hash.
 * Draws from PLACE_HUES (see above), a wheel disjoint from EVENT_HUES, so a
 * place's background wash can never land on the same hue as an unrelated
 * event block — and every stay block always carries its own text label
 * regardless.
 */
export function placeHue(stay) {
  const key =
    stay.place_id != null
      ? `place:${stay.place_id}`
      : stay.area_id != null
        ? `area:${stay.area_id}`
        : stay.lat != null && stay.lon != null
          ? `coord:${stay.lat.toFixed(3)},${stay.lon.toFixed(3)}`
          : null;
  if (key == null) return "var(--muted)";
  return PLACE_HUES[hashString(key) % PLACE_HUES.length];
}

// A plain polynomial rolling hash is linear in the trailing character, so
// sequential keys (place:1, place:2, place:3 — exactly what autoincrement
// place/area ids are) would land on a near-linear run of hashes instead of
// spreading across the wheel. The finalizer (Murmur3's fmix32) scrambles
// bits after accumulation so adjacent ids land on unrelated hues.
function hashString(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  h ^= h >>> 16;
  h = Math.imul(h, 0x85ebca6b);
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35);
  h ^= h >>> 16;
  return Math.abs(h);
}
