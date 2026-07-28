import { describe, it, expect } from "vitest";
import { placeClusterLabel, type LabelSpot } from "./hypergraphLabels";

// The real galaxy geometry from build_hypergraph_viz() in
// src/main_gpu_benchmark.rs: a 600×410 viewBox, k clusters on a ring of
// radius min(w,h)*0.33 centred in it, cluster_r from the ring spacing, and a
// halo of 1.35× cluster_r.
const W = 600;
const H = 410;

function galaxy(k: number) {
  const ring = Math.min(W, H) * 0.33;
  const clusterR = k <= 1
    ? Math.min(W, H) * 0.3
    : Math.min(ring * Math.sin(Math.PI / k) * 0.9, H * 0.22);
  const centroids = Array.from({ length: k }, (_, s) => {
    if (k === 1) return [W / 2, H / 2];
    const a = (2 * Math.PI * s) / k - Math.PI / 2;
    return [W / 2 + ring * Math.cos(a), H / 2 + ring * Math.sin(a)];
  });
  return { centroids, clusterR, haloR: clusterR * 1.35 };
}

const place = (k: number, index: number, textLen = 13): LabelSpot => {
  const g = galaxy(k);
  return placeClusterLabel({
    index, centroids: g.centroids, clusterR: g.clusterR, haloR: g.haloR,
    width: W, height: H, textLen,
  });
};

// A 12px label occupies ~[y-9, y+3] vertically and textLen*6.4 horizontally
// from its anchor point.
function box(s: LabelSpot, textLen: number) {
  const w = textLen * 6.4;
  const x0 = s.anchor === "start" ? s.x : s.anchor === "end" ? s.x - w : s.x - w / 2;
  return { x0, x1: x0 + w, y0: s.y - 9, y1: s.y + 3 };
}
const insideViewBox = (s: LabelSpot, textLen = 13) => {
  const b = box(s, textLen);
  return b.x0 >= 0 && b.x1 <= W && b.y0 >= 0 && b.y1 <= H;
};
function clearOfDots(s: LabelSpot, k: number, textLen = 13) {
  const g = galaxy(k);
  const b = box(s, textLen);
  return g.centroids.every(([cx, cy]) => {
    const nx = Math.min(b.x1, Math.max(b.x0, cx));
    const ny = Math.min(b.y1, Math.max(b.y0, cy));
    return Math.hypot(cx - nx, cy - ny) >= g.clusterR;
  });
}

describe("placeClusterLabel", () => {
  it("keeps every label of a full 8-cluster ring inside the viewBox", () => {
    // The bug: slot 0 (top) and slot 4 (bottom) used to land at y = -3.2 and
    // y = 415.2 — off-canvas, i.e. invisible.
    for (let s = 0; s < 8; s++) {
      expect(insideViewBox(place(8, s)), `slot ${s} escaped the viewBox`).toBe(true);
    }
  });

  it("never draws a label over any cluster's node dots", () => {
    for (const k of [2, 3, 4, 5, 6, 7, 8]) {
      for (let s = 0; s < k; s++) {
        expect(clearOfDots(place(k, s), k), `k=${k} slot ${s} sat on dots`).toBe(true);
        expect(insideViewBox(place(k, s)), `k=${k} slot ${s} escaped`).toBe(true);
      }
    }
  });

  it("sends left/right clusters out to the sides, away from the graph", () => {
    const g = galaxy(8);
    const right = place(8, 2);
    expect(right.anchor).toBe("start");
    expect(right.x).toBeGreaterThan(g.centroids[2][0] + g.haloR);

    const left = place(8, 6);
    expect(left.anchor).toBe("end");
    expect(left.x).toBeLessThan(g.centroids[6][0] - g.haloR);
  });

  it("puts the top and bottom clusters' labels above and below them", () => {
    const g = galaxy(8);
    const top = place(8, 0);
    expect(top.anchor).toBe("middle");
    expect(top.y).toBeLessThan(g.centroids[0][1]);

    const bottom = place(8, 4);
    expect(bottom.anchor).toBe("middle");
    expect(bottom.y).toBeGreaterThan(g.centroids[4][1]);
  });

  it("falls back to the side when there is no room above or below", () => {
    // k = 3: the clusters are huge, so the top one has no clear space above
    // it inside the canvas — the label has to go out to the side instead.
    const spot = place(3, 0);
    expect(spot.anchor).not.toBe("middle");
    expect(clearOfDots(spot, 3)).toBe(true);
    expect(insideViewBox(spot)).toBe(true);
  });

  it("pulls a wide label in rather than letting it clip at the edge", () => {
    const wide = place(8, 2, 30);
    expect(insideViewBox(wide, 30)).toBe(true);
  });

  it("still returns an on-canvas spot for a lone oversized cluster", () => {
    // k = 1 fills most of the canvas: no spot is fully clear, but the label
    // must still be visible rather than drawn outside the viewBox.
    const spot = place(1, 0);
    expect(insideViewBox(spot)).toBe(true);
  });
});
