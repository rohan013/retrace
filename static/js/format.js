/* Pure formatting helpers and shared reference data — no DOM, no state.
 *
 * The lane palette is the dataviz-validated 8-hue dark categorical set
 * (see palette.md), assigned in a fixed order across the kinds this app
 * actually sees. Lane identity is never carried by color alone: every lane
 * also has a fixed position and a text label, and every block shows its own
 * text — color reinforces, it doesn't replace either.
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

export function clockTime(ts, tz) {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
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

export function escapeHTML(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

export function hourLabel(hourOfDay) {
  const h = hourOfDay % 24;
  const period = h < 12 ? "am" : "pm";
  const display = h % 12 === 0 ? 12 : h % 12;
  return `${display}${period}`;
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
 * The dataviz-validated dark categorical set (palette.md), in its documented
 * order. Assigned once, by hand, below — never re-derived or cycled per
 * render.
 */

const CATEGORICAL_DARK = [
  "#3987e5", // 1 blue
  "#d95926", // 2 orange
  "#199e70", // 3 aqua
  "#c98500", // 4 yellow
  "#d55181", // 5 magenta
  "#008300", // 6 green
  "#9085e9", // 7 violet
  "#e66767", // 8 red
];

export const PLACE_COLOR = CATEGORICAL_DARK[0]; // blue — stays/trips

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
  reddit: "#FF6A33",
  // sites
  "youtube.com": "#FF5252",
  "reddit.com": "#FF6A33",
  "google.com": "#8AB4F8",
  "gemini.google.com": "#9AA9F5",
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
  return CATEGORICAL_DARK[hashString(key + fallbackSeed) % CATEGORICAL_DARK.length];
}

/* -- stable per-place colour -------------------------------------------------
 * Hashed from identity (place/area id), falling back to rounded coordinates
 * for an unnamed-but-located stay, and a neutral grey when nothing to hash.
 * Reuses the same 8-hue set lane accents draw from — the two never appear in
 * a context where confusing one for the other is possible, and every stay
 * block always carries its own text label regardless.
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
  return CATEGORICAL_DARK[hashString(key) % CATEGORICAL_DARK.length];
}

function hashString(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}
