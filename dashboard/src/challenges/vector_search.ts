import { DisplayPanelBase } from "./base";

interface VectorSearchData {
  num_queries: number;
  vector_dims: number;
  database_size: number;
  avg_distance: number | null;
}

type AllVectorSearchData = Record<string, VectorSearchData>;

// Earthen palette echoing hypergraph — cluster fills cycle through these so
// the schematic reads as part of the same dashboard language.
const CLUSTER_PALETTE = [
  "#B8541F", "#A66E45", "#C68F3E", "#6B7F4E",
  "#4A8C8A", "#4E6B85", "#8B6B8C", "#7A4F6E",
];

const VB_W = 600;
const VB_H = 360;

// Deterministic 32-bit hash of a string → uint32 seed for mulberry32.
// Used so the schematic stays stable across re-renders of the same instance,
// while different instance keys produce visibly different cluster layouts.
function hashString(s: string): number {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Box-Muller transform for one standard-normal sample from two uniforms.
function gauss(rng: () => number): number {
  const u = Math.max(rng(), 1e-9);
  const v = rng();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

export class VectorSearchPanel extends DisplayPanelBase<AllVectorSearchData> {
  protected idPrefix = "vs";

  private queriesEl!: HTMLElement;
  private distanceEl!: HTMLElement;
  private dbEl!: HTMLElement;
  private svgEl!: SVGSVGElement;
  private wrapEl!: HTMLElement;
  private bottomBarEl!: HTMLElement;
  private badgeEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner vs-panel">
        <div class="panel-label">VECTOR SEARCH</div>
        <div class="solution-agent-name" id="vs-agent-name"></div>
        ${this.navsScaffold()}
        <div class="vs-svg-wrap" id="vs-svg-wrap">
          <svg id="vs-svg" viewBox="0 0 ${VB_W} ${VB_H}" preserveAspectRatio="xMidYMid meet"></svg>
          <div class="vs-distance-badge" id="vs-distance-badge" style="display:none"></div>
          <div class="solution-empty-state" id="vs-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        ${this.statBarScaffold([
          { label: "QUERIES", id: "vs-queries" },
          { label: "DATABASE", id: "vs-db" },
          { label: "DIMS", value: "250D" },
          { label: "AVG DISTANCE", id: "vs-distance" },
        ])}
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.queriesEl = document.getElementById("vs-queries")!;
    this.distanceEl = document.getElementById("vs-distance")!;
    this.dbEl = document.getElementById("vs-db")!;
    this.svgEl = document.getElementById("vs-svg") as unknown as SVGSVGElement;
    this.wrapEl = document.getElementById("vs-svg-wrap")!;
    this.bottomBarEl = document.getElementById("vs-stat-bar")!;
    this.badgeEl = document.getElementById("vs-distance-badge")!;

    const resize = () => {
      this.svgEl.setAttribute("width", String(this.wrapEl.clientWidth));
      this.svgEl.setAttribute("height", String(this.wrapEl.clientHeight));
    };
    this.observeResize(this.wrapEl, resize);
    resize();
  }

  protected onReset(): void {
    this.queriesEl.textContent = "---";
    this.distanceEl.textContent = "---";
    this.dbEl.textContent = "---";
    this.svgEl.innerHTML = "";
    this.badgeEl.style.display = "none";
  }

  protected updateEmptyState() {
    super.updateEmptyState();
    const showEmpty = this.historyLoaded && this.historyEntries.length === 0;
    const display = showEmpty ? "none" : "";
    if (this.bottomBarEl) this.bottomBarEl.style.display = display;
    // Hide the SVG (and badge) when empty so the empty-state copy is the
    // only thing rendered in the wrap.
    if (this.svgEl) this.svgEl.style.display = display;
    if (this.badgeEl && showEmpty) this.badgeEl.style.display = "none";
  }

  protected showInstance(data: VectorSearchData) {
    if (!data) {
      this.onReset();
      return;
    }
    this.queriesEl.textContent = data.num_queries.toLocaleString();
    this.dbEl.textContent =
      `${data.database_size.toLocaleString()} × ${data.vector_dims}D`;
    this.distanceEl.textContent =
      data.avg_distance != null ? data.avg_distance.toFixed(4) : "---";

    const keys = this.instanceKeys;
    const instanceKey = keys[this.currentIndex] ?? "vs-default";
    this.renderSchematic(data, instanceKey);
  }

  // Illustrative 2-D projection of the challenge: a column of query points on
  // the left feeds nearest-neighbour hairlines into an anisotropic-Gaussian
  // cluster scatter on the right. The server payload only carries scalars
  // (num_queries, database_size, …) so the scatter is seeded from the
  // instance key — the picture stays stable across re-renders of the same
  // instance but changes between instances.
  private renderSchematic(data: VectorSearchData, instanceKey: string) {
    const rng = mulberry32(hashString(instanceKey));

    const padX = 24;
    const padY = 24;
    const queryColX = padX + 36;
    const dbAreaX0 = queryColX + 80;
    const dbAreaX1 = VB_W - padX;
    const dbAreaY0 = padY;
    const dbAreaY1 = VB_H - padY;
    const dbAreaW = dbAreaX1 - dbAreaX0;
    const dbAreaH = dbAreaY1 - dbAreaY0;

    // ── Query column ──
    const maxQueriesShown = 24;
    const queryCount = data.num_queries;
    const showQueryEllipsis = queryCount > maxQueriesShown;
    const queriesShown = Math.min(queryCount, maxQueriesShown);
    const slots = showQueryEllipsis ? queriesShown + 1 : queriesShown;
    const colTop = padY + 8;
    const colBot = VB_H - padY - 8;
    const colSpan = colBot - colTop;
    const queryYs: number[] = [];
    for (let i = 0; i < queriesShown; i++) {
      const t = slots > 1 ? i / (slots - 1) : 0.5;
      queryYs.push(colTop + t * colSpan);
    }
    const ellipsisY = showQueryEllipsis
      ? colTop + (queriesShown / (slots - 1)) * colSpan
      : null;

    // ── Database scatter ──
    // ~700 points per cluster on average per the README; clamp K to [3, 8]
    // so the schematic stays legible regardless of track size.
    const kRaw = Math.round(data.database_size / 700);
    const K = Math.max(3, Math.min(8, kRaw || 4));
    const clusterCenters: Array<{
      cx: number; cy: number; sx: number; sy: number; rot: number; color: string;
    }> = [];
    for (let k = 0; k < K; k++) {
      // Seed cluster centres inside the inner 70% of the DB area so their
      // tails rarely clip against the edge.
      const cx = dbAreaX0 + dbAreaW * (0.15 + 0.7 * rng());
      const cy = dbAreaY0 + dbAreaH * (0.15 + 0.7 * rng());
      const baseScale = Math.min(dbAreaW, dbAreaH) * 0.08;
      const sx = baseScale * (0.6 + 1.2 * rng());
      const sy = baseScale * (0.6 + 1.2 * rng());
      const rot = rng() * Math.PI;
      clusterCenters.push({
        cx, cy, sx, sy, rot,
        color: CLUSTER_PALETTE[k % CLUSTER_PALETTE.length],
      });
    }

    const totalDots = Math.min(data.database_size, 420);
    type DbDot = { x: number; y: number; color: string };
    const dbDots: DbDot[] = [];
    for (let i = 0; i < totalDots; i++) {
      const ck = clusterCenters[i % K];
      const z1 = gauss(rng);
      const z2 = gauss(rng);
      const cosR = Math.cos(ck.rot);
      const sinR = Math.sin(ck.rot);
      const x = ck.cx + (z1 * ck.sx) * cosR - (z2 * ck.sy) * sinR;
      const y = ck.cy + (z1 * ck.sx) * sinR + (z2 * ck.sy) * cosR;
      dbDots.push({ x, y, color: ck.color });
    }

    // ── Matches ── For each visible query, pick a "matched" DB index from a
    // weighted random pool biased toward the closer half (in screen space).
    // Purely visual narration of the nearest-neighbour assignment.
    const matchIdxs: number[] = [];
    for (let i = 0; i < queriesShown; i++) {
      const qy = queryYs[i];
      const pool: Array<{ idx: number; d2: number }> = [];
      const probeCount = Math.min(dbDots.length, 32);
      const start = Math.floor(rng() * Math.max(1, dbDots.length - probeCount));
      for (let p = 0; p < probeCount; p++) {
        const idx = (start + p) % dbDots.length;
        const d = dbDots[idx];
        const dx = d.x - queryColX;
        const dy = d.y - qy;
        pool.push({ idx, d2: dx * dx + dy * dy });
      }
      pool.sort((a, b) => a.d2 - b.d2);
      matchIdxs.push(pool[Math.floor(rng() * Math.min(3, pool.length))].idx);
    }

    // ── Emit SVG ──
    const parts: string[] = [];

    // Hairlines first so dots draw over them.
    for (let i = 0; i < queriesShown; i++) {
      const qy = queryYs[i];
      const m = dbDots[matchIdxs[i]];
      parts.push(
        `<line x1="${queryColX}" y1="${qy}" x2="${m.x.toFixed(2)}" y2="${m.y.toFixed(2)}" stroke="var(--color-accent)" stroke-width="0.4" stroke-opacity="0.55"/>`,
      );
    }

    // DB scatter.
    for (const d of dbDots) {
      parts.push(
        `<circle cx="${d.x.toFixed(2)}" cy="${d.y.toFixed(2)}" r="1.6" fill="${d.color}" fill-opacity="0.78"/>`,
      );
    }

    // Highlight the matched DB points.
    const seenMatches = new Set<number>();
    for (const mi of matchIdxs) {
      if (seenMatches.has(mi)) continue;
      seenMatches.add(mi);
      const d = dbDots[mi];
      parts.push(
        `<circle cx="${d.x.toFixed(2)}" cy="${d.y.toFixed(2)}" r="2.6" fill="none" stroke="var(--color-accent)" stroke-width="0.8" stroke-opacity="0.85"/>`,
      );
    }

    // Query column dots.
    for (const qy of queryYs) {
      parts.push(
        `<circle cx="${queryColX}" cy="${qy}" r="3" fill="var(--color-accent)"/>`,
      );
    }
    if (ellipsisY != null) {
      parts.push(
        `<text x="${queryColX}" y="${ellipsisY + 3}" text-anchor="middle" fill="var(--ink-dim)" font-size="11" font-family="var(--ui)">···</text>`,
      );
    }

    // Column label + tick.
    parts.push(
      `<text x="${queryColX}" y="${padY - 6}" text-anchor="middle" fill="var(--ink-dim)" font-size="9" font-family="var(--ui)" letter-spacing="0.12em">QUERIES</text>`,
    );
    parts.push(
      `<text x="${dbAreaX0 + dbAreaW / 2}" y="${padY - 6}" text-anchor="middle" fill="var(--ink-dim)" font-size="9" font-family="var(--ui)" letter-spacing="0.12em">DATABASE</text>`,
    );

    this.svgEl.innerHTML = parts.join("");

    // Distance badge.
    if (data.avg_distance != null) {
      this.badgeEl.style.display = "";
      this.badgeEl.textContent = `d̄ = ${data.avg_distance.toFixed(4)}`;
    } else {
      this.badgeEl.style.display = "none";
    }
  }
}
