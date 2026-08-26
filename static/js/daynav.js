/* The day bar, shared by the timeline and the breakdown.
 *
 * Both pages move between days and switch themes in exactly the same way, so
 * that wiring lives here rather than in each page's entry module. Everything
 * page-specific -- zoom presets, the raw-fix toggle, area drawing -- stays with
 * the page that owns it.
 */

import { THEME_LIST, setTheme, shiftDate, todayISO } from "./format.js";

const THEME_STORAGE_KEY = "retrace-theme";
const el = (id) => document.getElementById(id);

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** The day to open on: whatever the URL asks for, else today. */
export function initialDate() {
  const asked = new URLSearchParams(location.search).get("date");
  return asked && ISO_DATE.test(asked) ? asked : todayISO();
}

/** Wires the date controls and the theme picker.
 *
 * The day being viewed is written back into the URL and into the links across
 * to the other page, so a day is linkable, survives a reload, and follows you
 * when you switch views.
 *
 * The saved theme is applied synchronously, before this returns, so the first
 * paint already has the right palette instead of flashing the default one.
 */
export function initDayNav({ date, onDateChange, onThemeChange }) {
  let current = date;

  const publish = () => {
    const url = new URL(location.href);
    url.searchParams.set("date", current);
    history.replaceState(null, "", url);
    const timeline = el("nav-timeline");
    const breakdown = el("nav-breakdown");
    if (timeline) timeline.href = `/?date=${current}`;
    if (breakdown) breakdown.href = `/breakdown?date=${current}`;
  };

  const go = (iso) => {
    if (!iso || iso === current) return;
    current = iso;
    el("date").value = iso;
    publish();
    onDateChange(iso);
  };

  el("date").value = current;
  el("date").addEventListener("change", (event) => go(event.target.value));
  el("prev").addEventListener("click", () => go(shiftDate(current, -1)));
  el("next").addEventListener("click", () => go(shiftDate(current, 1)));
  el("today").addEventListener("click", () => go(todayISO()));

  document.addEventListener("keydown", (event) => {
    if (event.target.tagName === "INPUT") return;
    if (event.key === "ArrowLeft") go(shiftDate(current, -1));
    if (event.key === "ArrowRight") go(shiftDate(current, 1));
  });

  const themeSelect = el("theme-select");
  for (const { id, label } of THEME_LIST) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = label;
    themeSelect.appendChild(option);
  }
  const saved = localStorage.getItem(THEME_STORAGE_KEY) || "default";
  setTheme(saved);
  themeSelect.value = saved;
  themeSelect.addEventListener("change", () => {
    setTheme(themeSelect.value);
    localStorage.setItem(THEME_STORAGE_KEY, themeSelect.value);
    onThemeChange?.();
  });

  publish();
  return { current: () => current };
}
