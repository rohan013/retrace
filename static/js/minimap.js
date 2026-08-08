/* The rail left of the ruler: place/trip identity (colour + a vertical
 * name/duration label) mirroring the track's own pan and zoom exactly, so
 * whatever's legible in the track is the same size here. It has no
 * independent scale or scroll position of its own — every geometry input
 * comes from the track via setViewport()/setNow(), fired on every scroll,
 * zoom and resize.
 */

import { distance, duration, escapeHTML, placeHue, speedColour } from "./format.js";

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// Below this a band is too short for even the name to be worth attempting;
// below the second, the name shows but duration/distance doesn't fit too.
const NAME_MIN_PX = 28;
const DETAIL_MIN_PX = 90;

export function createMinimap(placesEl, { nowEl } = {}) {
  let currentDay = null;
  let lastScrollTop = 0;
  let lastViewportHeight = 0;
  let lastContentHeight = 0;
  let redrawRaf = null;

  // Same mapping as the track's own layoutYFor(ts, day, pxPerMinute), just
  // re-derived from contentHeight (the one thing setViewport gives us)
  // instead of pxPerMinute directly — the two are equivalent since
  // contentHeight is exactly layoutYFor(day.end_ts, ...).
  function contentYFor(ts) {
    const span = currentDay.end_ts - currentDay.start_ts;
    if (span <= 0) return 0;
    return ((ts - currentDay.start_ts) / span) * lastContentHeight;
  }

  function renderPlaces() {
    placesEl.innerHTML = "";
    if (!currentDay || lastViewportHeight <= 0) return;

    const items = currentDay.items.filter((i) => i.type === "stay" || i.type === "trip");
    for (const item of items) {
      const top = contentYFor(item.visible_start_ts) - lastScrollTop;
      const bottom = contentYFor(item.visible_end_ts) - lastScrollTop;
      if (bottom < 0 || top > lastViewportHeight) continue; // scrolled out of view
      const height = Math.max(1, bottom - top);

      const band = document.createElement("div");
      band.className = item.type === "trip" ? "minimap-band trip" : "minimap-band";
      band.style.top = `${top}px`;
      band.style.height = `${height}px`;
      band.style.setProperty(
        "--accent",
        item.type === "stay" ? placeHue(item) : speedColour(item.max_speed ?? item.avg_speed ?? 0)
      );
      if (height >= NAME_MIN_PX) {
        const name = item.type === "stay" ? item.name || "Unnamed place" : "Moving";
        const detailHTML =
          height >= DETAIL_MIN_PX
            ? `<span class="rail-detail">${
                item.type === "stay" ? duration(item.visible_duration_s) : distance(item.distance_m)
              }</span>`
            : "";
        band.innerHTML = `<span class="rail-name">${escapeHTML(name)}</span>${detailHTML}`;
      }
      placesEl.appendChild(band);
    }
  }

  // Scroll fires far more often than a redraw needs to happen — batched to
  // one rebuild per frame, mirroring how the track throttles its own ruler.
  function scheduleRedraw() {
    if (redrawRaf) return;
    redrawRaf = requestAnimationFrame(() => {
      redrawRaf = null;
      renderPlaces();
    });
  }

  return {
    draw(day) {
      currentDay = day;
      scheduleRedraw();
    },
    /** Called whenever the track's scroll position, viewport height or total
     * content height changes (zoom, scroll, resize) — the rail has no scale
     * or position of its own, so every one of these needs a redraw. */
    setViewport(scrollTop, viewportHeight, contentHeight) {
      lastScrollTop = scrollTop;
      lastViewportHeight = viewportHeight;
      lastContentHeight = contentHeight;
      scheduleRedraw();
    },
    /** ts is the current time, or null when the day being viewed does not
     * contain it — hidden either then or whenever "now" has scrolled out of
     * the currently visible slice. */
    setNow(ts) {
      if (!nowEl) return;
      if (ts == null || !currentDay) {
        nowEl.hidden = true;
        return;
      }
      const y = contentYFor(ts) - lastScrollTop;
      if (y < 0 || y > lastViewportHeight) {
        nowEl.hidden = true;
        return;
      }
      nowEl.hidden = false;
      nowEl.style.top = `${clamp(y, 0, lastViewportHeight)}px`;
    },
  };
}
