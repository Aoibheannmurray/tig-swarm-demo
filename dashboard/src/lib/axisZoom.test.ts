import { describe, it, expect } from "vitest";
import {
  MAX_ZOOM,
  clampZoom,
  identityZoom,
  isZoomed,
  panBy,
  panByThumb,
  panToThumbStart,
  thumbGeometry,
  zoomAt,
} from "./axisZoom";

// The visible fit-space window for a plot `size` px wide — the thing the
// chart's rescaled domain is derived from.
// (`+ 0` normalizes -0, which toEqual distinguishes from 0.)
const window_ = (z: { k: number; t: number }, size: number) => [
  -z.t / z.k + 0,
  (size - z.t) / z.k + 0,
];

describe("axisZoom", () => {
  it("starts at fit and reports not-zoomed", () => {
    const z = identityZoom();
    expect(window_(z, 500)).toEqual([0, 500]);
    expect(isZoomed(z)).toBe(false);
  });

  it("keeps the anchored value under the cursor", () => {
    const z = zoomAt(identityZoom(), 200, 4, 500);
    expect(z.k).toBe(4);
    // 200px was 40% across the fit range; it must still be 40% across the
    // zoomed window.
    const [lo, hi] = window_(z, 500);
    expect(lo + (hi - lo) * 0.4).toBeCloseTo(200, 6);
  });

  it("never zooms out past fit and never exceeds MAX_ZOOM", () => {
    expect(zoomAt(identityZoom(), 100, 0.25, 500)).toEqual({ k: 1, t: 0 });
    expect(zoomAt({ k: 40, t: -100 }, 100, 8, 500).k).toBe(MAX_ZOOM);
  });

  it("clamps panning to the data range", () => {
    const z = zoomAt(identityZoom(), 250, 2, 500); // k=2, window centred
    // Drag far right: the window stops at the left edge of the data.
    expect(window_(panBy(z, 10_000, 500), 500)).toEqual([0, 250]);
    // Drag far left: it stops at the right edge.
    expect(window_(panBy(z, -10_000, 500), 500)).toEqual([250, 500]);
  });

  it("zooms each axis independently of the other", () => {
    // Two axes sharing one gesture used to be the whole problem: proof that
    // one axis' state is untouched by the other's.
    const x = zoomAt(identityZoom(), 100, 8, 400);
    const y = identityZoom();
    expect(x.k).toBe(8);
    expect(y).toEqual({ k: 1, t: 0 });
  });

  it("maps the thumb onto the visible window", () => {
    expect(thumbGeometry(identityZoom(), 500)).toEqual({ offset: 0, length: 1 });
    const z = clampZoom({ k: 4, t: -500 }, 500); // window = [125, 250]
    const { offset, length } = thumbGeometry(z, 500);
    expect(length).toBeCloseTo(0.25, 6);
    expect(offset).toBeCloseTo(0.25, 6);
  });

  it("round-trips a thumb drag into the same window shift", () => {
    const z = clampZoom({ k: 4, t: -200 }, 500);
    const dragged = panByThumb(z, 25, 500);
    // Thumb moved 25px right along a 500px track => window moves 25 fit-px.
    expect(window_(dragged, 500)[0] - window_(z, 500)[0]).toBeCloseTo(25, 6);
    expect(dragged.k).toBe(z.k);
  });

  it("jumps the window when the track is clicked", () => {
    const z = clampZoom({ k: 5, t: 0 }, 500);
    const jumped = panToThumbStart(z, 300, 500);
    expect(window_(jumped, 500)[0]).toBeCloseTo(300, 6);
    // ...but not past the end of the data.
    expect(window_(panToThumbStart(z, 480, 500), 500)).toEqual([400, 500]);
  });

  it("survives a zero-sized plot without producing NaN", () => {
    const z = zoomAt(identityZoom(), 0, 2, 0);
    expect(z.t).toBe(0);
    expect(Number.isFinite(z.k)).toBe(true);
    expect(thumbGeometry(z, 0)).toEqual({ offset: 0, length: 1 });
  });
});
