/* Block geometry for one lane: projection, concurrency packing, and
 * proximity clustering. Pure — no DOM, so it's the one module worth testing
 * directly against real day payloads.
 *
 * Two phases, deliberately in this order:
 *
 *  1. PACK by real time overlap (interval-graph greedy colouring) into
 *     sub-columns. Two devices reporting the same kind at the same moment
 *     land side by side, never silently merged into one block that hides
 *     one of them.
 *  2. CLUSTER within each sub-column by pixel proximity at the current zoom.
 *     Items too close together to render individually collapse into one
 *     labelled cluster block that dissolves back into singles as
 *     pxPerMinute rises.
 *
 * Running clustering before packing would merge concurrent-but-distinct
 * items (e.g. two devices' overlapping wifi ranges) into one block purely
 * because they're close in time — exactly what the combined device view
 * must not do.
 */

export const MIN_BLOCK_PX = 4; // nothing is ever thinner than this
export const MERGE_GAP_PX = 3; // blocks closer than this collapse together
export const LABEL_MIN_PX = 16; // a one-line label fits
export const FULL_LABEL_MIN_PX = 52; // the three-line label fits

export const yFor = (ts, day, pxPerMinute) => ((ts - day.start_ts) / 60) * pxPerMinute;

function itemEnd(item) {
  return item.visible_end_ts ?? item.visible_start_ts;
}

/** Greedy interval-graph colouring: each item takes the first sub-column
 * whose last-placed item has already ended. Returns columns in the order
 * they were first used, each an array of items sorted by start. */
function packColumns(items) {
  const sorted = [...items].sort((a, b) => a.visible_start_ts - b.visible_start_ts);
  const columns = []; // { endTs, items }
  for (const item of sorted) {
    const col = columns.find((c) => c.endTs <= item.visible_start_ts);
    if (col) {
      col.items.push(item);
      col.endTs = Math.max(col.endTs, itemEnd(item));
    } else {
      columns.push({ endTs: itemEnd(item), items: [item] });
    }
  }
  return columns.map((c) => c.items);
}

function clusterLabel(item) {
  if (item.type === "stay") return item.name || "Unnamed place";
  if (item.type === "trip") return "Moving";
  return item.subject || item.kind;
}

function buildCluster(members) {
  const top = Math.min(...members.map((m) => m.top));
  const bottom = Math.max(...members.map((m) => m.bottom));
  const totals = new Map();
  for (const { item } of members) {
    const key = clusterLabel(item);
    const weight = Math.max(1, itemEnd(item) - item.visible_start_ts);
    const entry = totals.get(key) || { key, count: 0, weight: 0 };
    entry.count += 1;
    entry.weight += weight;
    totals.set(key, entry);
  }
  const histogram = [...totals.values()].sort((a, b) => b.weight - a.weight);
  return {
    kind: "cluster",
    items: members.map((m) => m.item),
    histogram,
    top,
    bottom,
    height: bottom - top,
  };
}

/** Sweep one sub-column (already non-overlapping in real time) and collapse
 * runs of unreadably-short blocks into clusters.
 *
 * Two conditions must BOTH hold to merge: the blocks are individually too short
 * to carry a label, *and* they are close enough to collide on screen. Merging on
 * proximity alone was a bug — focus events abut exactly (one app's focus ends the
 * instant the next begins), so a gap-only rule merged them at every zoom level
 * and no amount of zooming ever separated them. Keying on height means a block
 * splits out of its cluster the moment it is big enough to read, which is what
 * makes zooming in actually reveal individual app switches.
 */
function clusterColumn(items, day, pxPerMinute) {
  const projected = items
    .map((item) => {
      const top = yFor(item.visible_start_ts, day, pxPerMinute);
      const natural = yFor(itemEnd(item), day, pxPerMinute) - top;
      return {
        item,
        top,
        bottom: Math.max(top + MIN_BLOCK_PX, top + natural),
        tiny: natural < LABEL_MIN_PX,
      };
    })
    .sort((a, b) => a.top - b.top);

  const runs = [];
  let run = null;
  for (const p of projected) {
    const collides = run && p.top < run.bottom + MERGE_GAP_PX;
    if (run && run.tiny && p.tiny && collides) {
      run.members.push(p);
      run.bottom = Math.max(run.bottom, p.bottom);
    } else {
      run = { bottom: p.bottom, tiny: p.tiny, members: [p] };
      runs.push(run);
    }
  }

  const blocks = runs.map((r) =>
    r.members.length === 1
      ? {
          kind: "single",
          item: r.members[0].item,
          top: r.members[0].top,
          bottom: r.members[0].bottom,
          height: r.members[0].bottom - r.members[0].top,
        }
      : buildCluster(r.members)
  );

  // The MIN_BLOCK_PX floor can push a short block past the start of the next
  // one — a one-second blip immediately before an hours-long session, say.
  // Trim rather than overlap: a sliver must never cover its neighbour.
  //
  // No minimum height is enforced here on purpose. When two items are a fifth
  // of a pixel apart there is no honest way to draw both at their true
  // positions, and inflating the small one would misreport when the large one
  // began. Such an item stays in the DOM (so it keeps its tooltip and stays
  // selectable) and becomes visible as soon as the zoom makes it real — which
  // is exactly what the zoom presets are for.
  for (let i = 0; i < blocks.length - 1; i++) {
    if (blocks[i].bottom > blocks[i + 1].top) {
      blocks[i].bottom = Math.max(blocks[i].top, blocks[i + 1].top);
      blocks[i].height = blocks[i].bottom - blocks[i].top;
    }
  }
  return blocks;
}

function layoutGroup(items, day, pxPerMinute) {
  const columns = packColumns(items);
  const cols = columns.length;
  const blocks = [];
  columns.forEach((colItems, col) => {
    for (const block of clusterColumn(colItems, day, pxPerMinute)) {
      block.col = col;
      block.cols = cols;
      blocks.push(block);
    }
  });
  return blocks;
}

/** items: this lane's slice of day.items. Returns blocks ready to render,
 * each carrying {top, bottom, height, col, cols, group} plus either
 * {kind:"single", item} or {kind:"cluster", items, histogram}.
 *
 * `groups` optionally splits one lane into named sections that pack and
 * cluster independently — how the focus lane keeps a browser site from ever
 * merging into the same cluster as the app block it's nested inside. Pass
 * `[{key, of}]`; `col`/`cols` stay relative to the group, and the caller
 * decides how each group is positioned.
 */
export function layoutLane(items, day, pxPerMinute, groups = null) {
  if (!items.length) return [];
  if (!groups) return layoutGroup(items, day, pxPerMinute).map((b) => ((b.group = null), b));

  const blocks = [];
  for (const group of groups) {
    const mine = items.filter(group.of);
    if (!mine.length) continue;
    for (const block of layoutGroup(mine, day, pxPerMinute)) {
      block.group = group.key;
      blocks.push(block);
    }
  }
  return blocks;
}

/** Per-row coverage fraction (0..1) for a lane's items, mapped onto a rail
 * of `height` pixels spanning the whole day — used by the minimap and by a
 * collapsed lane's heat strip. Presence-based: any covering item saturates a
 * row toward 1, so density is legible without per-row occlusion. */
export function densityBuckets(items, day, height) {
  const buckets = new Float32Array(height);
  const span = day.end_ts - day.start_ts;
  if (!items.length || height <= 0 || span <= 0) return buckets;
  const secondsPerRow = span / height;

  for (const item of items) {
    const start = Math.max(item.visible_start_ts, day.start_ts);
    const end = Math.min(Math.max(itemEnd(item), start + secondsPerRow * 0.2), day.end_ts);
    const rowStart = Math.max(0, Math.floor((start - day.start_ts) / secondsPerRow));
    const rowEnd = Math.min(height, Math.max(rowStart + 1, Math.ceil((end - day.start_ts) / secondsPerRow)));
    for (let row = rowStart; row < rowEnd; row++) {
      buckets[row] = Math.min(1, buckets[row] + 0.6);
    }
  }
  return buckets;
}
