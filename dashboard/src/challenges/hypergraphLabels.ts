// Where a hypergraph cluster's "p<n> · <size>" label goes.
//
// The clusters sit on a ring whose diameter plus halo very nearly fills the
// viewBox, so the old "above the centroid if it is in the top half, below it
// otherwise" rule pushed the top and bottom clusters' labels clean off the
// canvas — they were simply invisible. Each label now tries a series of spots
// around its own cluster, outermost direction first, and takes the first one
// that fits in the viewBox without landing on anybody's node dots: out to the
// side for the left/right clusters (where the free space is), above/below for
// the top/bottom ones, and the other way round when that is what fits.
//
// Kept pure and DOM-free so the geometry is unit-testable.

export interface LabelSpot {
  x: number;
  y: number;
  anchor: "start" | "middle" | "end";
}

export interface LabelPlacementInput {
  index: number;                  // which cluster this label belongs to
  centroids: number[][];          // every cluster centroid, [[cx, cy], ...]
  clusterR: number;               // node-dot disk radius (labels avoid these)
  haloR: number;                  // halo radius (labels sit outside it)
  width: number;                  // viewBox
  height: number;
  textLen: number;                // label length in characters
}

// Gap between the halo edge and the text.
const PAD = 8;
// Keep text this far inside the viewBox.
const MARGIN = 4;
// 12px UI font: cap height above the baseline, descender below, and average
// advance width. Only used for fitting, so approximate is fine — but err on
// the generous side so a label is pulled in rather than clipped.
const CAP = 9;
const DESC = 3;
const CHAR_W = 6.4;

const clamp = (v: number, lo: number, hi: number) =>
  hi < lo ? (lo + hi) / 2 : Math.min(hi, Math.max(lo, v));
const r1 = (v: number) => Math.round(v * 10) / 10;

// The box a label occupies, given its anchor point.
function labelBox(spot: LabelSpot, textW: number) {
  const x0 = spot.anchor === "start" ? spot.x
    : spot.anchor === "end" ? spot.x - textW
    : spot.x - textW / 2;
  return { x0, x1: x0 + textW, y0: spot.y - CAP, y1: spot.y + DESC };
}

function clampIntoBox(spot: LabelSpot, textW: number, width: number, height: number): LabelSpot {
  const y = clamp(spot.y, MARGIN + CAP, height - MARGIN - DESC);
  let x = spot.x;
  if (spot.anchor === "start") x = clamp(x, MARGIN, width - MARGIN - textW);
  else if (spot.anchor === "end") x = clamp(x, MARGIN + textW, width - MARGIN);
  else x = clamp(x, MARGIN + textW / 2, width - MARGIN - textW / 2);
  return { x, y, anchor: spot.anchor };
}

// Does the label box land on any cluster's node dots?
function hitsDots(spot: LabelSpot, textW: number, centroids: number[][], r: number): boolean {
  const b = labelBox(spot, textW);
  for (const [cx, cy] of centroids) {
    const nx = clamp(cx, b.x0, b.x1);
    const ny = clamp(cy, b.y0, b.y1);
    if (Math.hypot(cx - nx, cy - ny) < r) return true;
  }
  return false;
}

export function placeClusterLabel(o: LabelPlacementInput): LabelSpot {
  const [cx, cy] = o.centroids[o.index];
  const dx = cx - o.width / 2;
  const dy = cy - o.height / 2;
  const textW = o.textLen * CHAR_W;
  const out = o.haloR + PAD;

  const right: LabelSpot = { x: cx + out, y: cy + CAP / 2, anchor: "start" };
  const left: LabelSpot = { x: cx - out, y: cy + CAP / 2, anchor: "end" };
  const above: LabelSpot = { x: cx, y: cy - out, anchor: "middle" };
  const below: LabelSpot = { x: cx, y: cy + out + CAP, anchor: "middle" };

  // Try the outward direction first, then the other axis (nearer side / freer
  // vertical), then the two inward ones as a last resort.
  const sideFirst = dx >= 0 ? [right, left] : [left, right];
  const vertFirst = dy < 0 ? [above, below] : [below, above];
  const candidates = Math.abs(dx) >= Math.abs(dy)
    ? [...sideFirst.slice(0, 1), ...vertFirst, ...sideFirst.slice(1)]
    : [...vertFirst.slice(0, 1), ...sideFirst, ...vertFirst.slice(1)];

  // A spot may be pulled back inside the viewBox — the top cluster's label,
  // for instance, ends up just over its own (7%-opacity) halo rather than off
  // the canvas. What it must not do is get dragged past the centroid onto the
  // far side of the cluster; that reads as a label for whatever is over there.
  const staysOutward = (c: LabelSpot, fitted: LabelSpot) => {
    if (c.anchor === "middle") return c.y < cy ? fitted.y <= cy : fitted.y >= cy;
    return c.anchor === "start" ? fitted.x >= cx : fitted.x <= cx;
  };

  for (const c of candidates) {
    const fitted = clampIntoBox(c, textW, o.width, o.height);
    if (staysOutward(c, fitted) && !hitsDots(fitted, textW, o.centroids, o.clusterR)) {
      return { x: r1(fitted.x), y: r1(fitted.y), anchor: fitted.anchor };
    }
  }

  // Nothing was completely clear (very large clusters relative to the canvas):
  // take the outward-most spot clamped into view. Overlapping some dots is far
  // better than the label being off-canvas, which is what used to happen.
  const best = clampIntoBox(candidates[0], textW, o.width, o.height);
  return { x: r1(best.x), y: r1(best.y), anchor: best.anchor };
}
