// One-dimensional pan/zoom state for a single chart axis.
//
// The benchmark chart used to drive both axes off one d3-zoom transform, so
// every wheel notch scaled x and y by the same factor — there was no way to
// stretch a dense score band without also stretching time, and no scrollbar
// to show where the zoomed window sat. Each axis now carries its own
// {k, t} and this module holds all of the math (chart.ts stays DOM +
// drawing), which also makes it unit-testable without a browser.
//
// Convention: `k` is the zoom factor (1 = fit) and `t` is the pixel offset of
// the zoomed range's origin, matching d3-zoom's transform. A pixel `p` in the
// fit range maps to `p * k + t`, so the window visible in a plot `size` px
// wide is [-t/k, (size - t)/k] in fit-space pixels.

export interface AxisZoom {
  k: number;
  t: number;
}

export const MIN_ZOOM = 1;
export const MAX_ZOOM = 64;

export function identityZoom(): AxisZoom {
  return { k: 1, t: 0 };
}

export function isZoomed(z: AxisZoom): boolean {
  return z.k > 1.001;
}

// Clamp to the legal window: never zoomed out past fit (k >= 1) and never
// panned so far that the plot shows empty gutter on either side.
export function clampZoom(z: AxisZoom, size: number): AxisZoom {
  const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z.k || 1));
  if (!(size > 0)) return { k, t: 0 };
  const t = Math.min(0, Math.max(size * (1 - k), z.t || 0));
  return { k, t };
}

// Zoom by `factor` about `anchor` (a pixel offset inside the plot box), so the
// value under the cursor stays put.
export function zoomAt(
  z: AxisZoom, anchor: number, factor: number, size: number,
): AxisZoom {
  if (!Number.isFinite(factor) || factor <= 0) return clampZoom(z, size);
  const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z.k * factor));
  const applied = k / z.k;
  return clampZoom({ k, t: anchor - (anchor - z.t) * applied }, size);
}

// Pan by a pixel delta in screen space (drag direction = data direction).
export function panBy(z: AxisZoom, delta: number, size: number): AxisZoom {
  return clampZoom({ k: z.k, t: z.t + delta }, size);
}

// Where the scrollbar thumb sits, as fractions of the track: `offset` is its
// start, `length` its size. At k = 1 the thumb fills the track.
export function thumbGeometry(
  z: AxisZoom, size: number,
): { offset: number; length: number } {
  if (!(size > 0) || !(z.k > 0)) return { offset: 0, length: 1 };
  const length = Math.min(1, 1 / z.k);
  const offset = Math.min(1 - length, Math.max(0, -z.t / (z.k * size)));
  return { offset, length };
}

// Dragging the thumb `delta` px along the track moves the window the other
// way, scaled by the zoom factor (the track spans the whole fit range).
export function panByThumb(
  z: AxisZoom, delta: number, size: number,
): AxisZoom {
  return clampZoom({ k: z.k, t: z.t - delta * z.k }, size);
}

// Jump the window so the thumb starts at `startPx` along the track — used when
// clicking the track outside the thumb.
export function panToThumbStart(
  z: AxisZoom, startPx: number, size: number,
): AxisZoom {
  return clampZoom({ k: z.k, t: -startPx * z.k }, size);
}
