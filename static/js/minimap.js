/* The always-visible 24h rail: a canvas mapping the whole day onto a fixed
 * height (independent of the track's own zoom), a place band, one thin
 * density strip per event lane, and a draggable viewport indicator that
 * drives the track's scroll position. Never scrolls itself.
 */

import { hexToRgb, laneMeta, LANE_ORDER, placeHue, speedColour } from "./format.js";
import { densityBuckets } from "./layout.js";

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export function createMinimap(canvas, viewportEl, { onSeek } = {}) {
  const ctx = canvas.getContext("2d");
  let currentDay = null;
  let lastContentHeight = 0;
  let lastViewportHeight = 0;

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (currentDay) draw(currentDay);
    updateViewportRect();
  }

  function draw(day) {
    currentDay = day;
    const rect = canvas.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);
    if (!day || h <= 0 || w <= 0) return;

    const span = day.end_ts - day.start_ts;
    if (span <= 0) return;
    const yFor = (ts) => ((clamp(ts, day.start_ts, day.end_ts) - day.start_ts) / span) * h;

    const placeItems = day.items.filter((i) => i.type === "stay" || i.type === "trip");
    // `session` has no lane in the track, so it gets no strip here either —
    // the rail should be a map of what the track shows.
    const eventKinds = [
      ...new Set(
        day.items.filter((i) => i.type === "event" && i.kind !== "session").map((i) => i.kind)
      ),
    ].sort((a, b) => LANE_ORDER.indexOf(a) - LANE_ORDER.indexOf(b));

    const placeWidth = eventKinds.length ? w * 0.44 : w;

    for (const item of placeItems) {
      const y0 = yFor(item.visible_start_ts);
      const y1 = Math.max(y0 + 1, yFor(item.visible_end_ts));
      ctx.globalAlpha = item.type === "stay" ? 0.9 : 0.65;
      ctx.fillStyle =
        item.type === "stay" ? placeHue(item) : speedColour(item.max_speed ?? item.avg_speed ?? 0);
      ctx.fillRect(0, y0, placeWidth, y1 - y0);
    }
    ctx.globalAlpha = 1;

    if (eventKinds.length) {
      const stripWidth = (w - placeWidth) / eventKinds.length;
      const rows = Math.max(1, Math.round(h));
      eventKinds.forEach((kind, i) => {
        const items = day.items.filter((it) => it.type === "event" && it.kind === kind);
        const buckets = densityBuckets(items, day, rows);
        const [r, g, b] = hexToRgb(laneMeta(kind).color);
        const x = placeWidth + i * stripWidth;
        for (let row = 0; row < rows; row++) {
          const a = buckets[row];
          if (a <= 0) continue;
          ctx.fillStyle = `rgba(${r},${g},${b},${0.18 + a * 0.72})`;
          ctx.fillRect(x, row, Math.max(1, stripWidth - 1), 1);
        }
      });
    }

    // Hairlines every 6h — text would be illegible at this width; the
    // track's own ruler already carries hour labels.
    ctx.strokeStyle = "rgba(255,255,255,0.10)";
    ctx.lineWidth = 1;
    const totalHours = span / 3600;
    for (let hr = 6; hr < totalHours; hr += 6) {
      const y = Math.round((hr * 3600 * h) / span) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }
  }

  function updateViewportRect() {
    const rect = canvas.getBoundingClientRect();
    const h = rect.height;
    if (lastContentHeight <= 0 || h <= 0) {
      viewportEl.style.height = "0px";
      return;
    }
    const top = clamp((lastScrollTop / lastContentHeight) * h, 0, h);
    const height = Math.max(8, (lastViewportHeight / lastContentHeight) * h);
    viewportEl.style.top = `${top}px`;
    viewportEl.style.height = `${Math.min(height, h - top)}px`;
  }

  let lastScrollTop = 0;

  function railYToScrollTop(clientY) {
    const rect = canvas.getBoundingClientRect();
    const frac = clamp((clientY - rect.top) / rect.height, 0, 1);
    const target = frac * lastContentHeight - lastViewportHeight / 2;
    return clamp(target, 0, Math.max(0, lastContentHeight - lastViewportHeight));
  }

  let dragOffset = null;
  function onDragMove(e) {
    const railRect = canvas.getBoundingClientRect();
    const railTop = e.clientY - dragOffset - railRect.top;
    const frac = railTop / railRect.height;
    const scrollTop = clamp(
      frac * lastContentHeight,
      0,
      Math.max(0, lastContentHeight - lastViewportHeight)
    );
    onSeek?.(scrollTop);
  }
  function onDragEnd() {
    dragOffset = null;
    window.removeEventListener("mousemove", onDragMove);
    window.removeEventListener("mouseup", onDragEnd);
  }

  viewportEl.addEventListener("mousedown", (e) => {
    e.preventDefault();
    dragOffset = e.clientY - viewportEl.getBoundingClientRect().top;
    window.addEventListener("mousemove", onDragMove);
    window.addEventListener("mouseup", onDragEnd);
  });

  canvas.addEventListener("mousedown", (e) => {
    onSeek?.(railYToScrollTop(e.clientY));
  });

  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  window.addEventListener("resize", resize);

  return {
    draw,
    /** Called whenever the track's scroll position or total content height
     * changes (zoom, scroll, resize) to keep the viewport rectangle honest. */
    setViewport(scrollTop, viewportHeight, contentHeight) {
      lastScrollTop = scrollTop;
      lastViewportHeight = viewportHeight;
      lastContentHeight = contentHeight;
      updateViewportRect();
    },
  };
}
