import { select, type Selection } from "d3-selection";
import { DisplayPanelBase } from "./base";

interface GalaxyView {
  shown_partitions: number[];
  shown_sizes: number[];
  over_cap: boolean[];
  centroids: number[][];       // [[cx, cy], ...] × 8
  cluster_r: number;           // radius used for halo sizing
  nodes: number[][];           // [[slot, x, y], ...] × ~2000
  cut_edges: number[][];       // each entry: [nodeIdx, nodeIdx, ...]
  width: number;
  height: number;
}

interface HypergraphData {
  num_nodes: number;
  num_parts: number;
  max_part_size: number;
  partition_sizes: number[];
  cuts_between?: number[][];
  connectivity_metric: number | null;
  baseline_connectivity_metric: number;
  galaxy_view?: GalaxyView;
}

type AllHypergraphData = Record<string, HypergraphData>;

// Earthen TIG palette — each slot picks one. No outside colours allowed,
// so cut ribbons and the over-cap warning ring also draw from this list
// (just with different opacity / dash / stroke weight to stay visually
// distinct from the partitions they mark up).
const PALETTE = [
  "#B8541F",   // burnt sienna
  "#A66E45",   // warm taupe
  "#C68F3E",   // mustard gold
  "#6B7F4E",   // olive green
  "#4A8C8A",   // teal
  "#4E6B85",   // slate blue
  "#8B6B8C",   // mauve
  "#7A4F6E",   // plum
];

// Burnt sienna doubles as the "warning" tint for over-cap halos — it's the
// hottest hue in the palette and dashed + pulsing makes it read as a status
// indicator, not a fill colour, even when it overlaps slot 0's own dots.
const VIOLATION_COLOR = "#B8541F";
// Plum for cut ribbons — quiet, cool, and unlikely to be confused with the
// cluster fills since cuts are 0.5px hairlines and clusters are 1.6px dots.
const CUT_COLOR = "#7A4F6E";

const FALLBACK_VB_W = 600;
const FALLBACK_VB_H = 410;

export class HypergraphPanel extends DisplayPanelBase<AllHypergraphData> {
  protected idPrefix = "hg";

  private svg!: Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: Selection<SVGGElement, unknown, HTMLElement, any>;
  private nodesEl!: HTMLElement;
  private partsEl!: HTMLElement;
  private metricEl!: HTMLElement;
  private cutsEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner hg-panel">
        <div class="panel-label">PARTITION</div>
        <div class="solution-agent-name" id="hg-agent-name"></div>
        ${this.navsScaffold()}
        <div class="hg-svg-wrap" id="hg-svg-wrap">
          <svg id="hg-svg"></svg>
          <div class="solution-empty-state" id="hg-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="hg-bottom-bar">
          <div class="hg-stat">
            <div class="hg-stat-label">NODES</div>
            <div class="hg-stat-value" id="hg-nodes">---</div>
          </div>
          <div class="hg-stat">
            <div class="hg-stat-label">PARTS</div>
            <div class="hg-stat-value" id="hg-parts">---</div>
          </div>
          <div class="hg-stat">
            <div class="hg-stat-label">CUTS</div>
            <div class="hg-stat-value" id="hg-cuts">---</div>
          </div>
          <div class="hg-stat">
            <div class="hg-stat-label">CONNECTIVITY</div>
            <div class="hg-stat-value" id="hg-metric">---</div>
          </div>
          <div class="hg-stat hg-stat-score">
            <div class="hg-stat-label">SCORE</div>
            <div class="hg-stat-value" id="hg-score">---</div>
            <div class="hg-stat-delta" id="hg-score-delta"></div>
          </div>
        </div>
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.scoreEl = document.getElementById("hg-score")!;
    this.scoreDeltaEl = document.getElementById("hg-score-delta")!;
    this.instanceLabelEl = document.getElementById("hg-instance-label")!;
    this.navEl = document.getElementById("hg-nav")!;
    this.agentNameEl = document.getElementById("hg-agent-name")!;
    this.historyNavEl = document.getElementById("hg-history-nav")!;
    this.historyLabelEl = document.getElementById("hg-history-label")!;
    this.historyLiveBtnEl = document.getElementById("hg-hist-live")!;
    this.emptyStateEl = document.getElementById("hg-empty-state")!;

    this.nodesEl = document.getElementById("hg-nodes")!;
    this.partsEl = document.getElementById("hg-parts")!;
    this.metricEl = document.getElementById("hg-metric")!;
    this.cutsEl = document.getElementById("hg-cuts")!;

    this.svg = select("#hg-svg") as any;
    this.svg
      .attr("viewBox", `0 0 ${FALLBACK_VB_W} ${FALLBACK_VB_H}`)
      .attr("preserveAspectRatio", "xMidYMid meet");

    this.chartG = this.svg.append("g").attr("class", "hg-chart") as any;

    const wrap = document.getElementById("hg-svg-wrap")!;
    const resize = () => {
      this.svg.attr("width", wrap.clientWidth).attr("height", wrap.clientHeight);
    };
    this.observeResize(wrap, resize);
    resize();
  }

  protected onReset(): void {
    (this.chartG.node() as SVGGElement).innerHTML = "";
    this.nodesEl.textContent = "---";
    this.partsEl.textContent = "---";
    this.metricEl.textContent = "---";
    this.cutsEl.textContent = "---";
  }

  protected showInstance(data: HypergraphData) {
    if (!data) {
      this.onReset();
      return;
    }

    // ── Stats bar ──
    // NODES and PARTS display the sampled subset so the values track what's
    // actually on screen, not the (much larger) full graph.
    const gv = data.galaxy_view;
    const sampledNodes = gv?.nodes.length ?? data.num_nodes;
    const shownParts = gv?.shown_partitions.length ?? data.num_parts;
    this.nodesEl.textContent = sampledNodes.toLocaleString();
    this.partsEl.textContent = gv
      ? `${shownParts} / ${data.num_parts}`
      : `${data.num_parts}`;

    let totalCuts = 0;
    if (data.cuts_between) {
      for (let i = 0; i < data.cuts_between.length; i++) {
        for (let j = i + 1; j < data.cuts_between.length; j++) {
          totalCuts += data.cuts_between[i]?.[j] ?? 0;
        }
      }
    }
    this.cutsEl.textContent = totalCuts > 0 ? totalCuts.toLocaleString() : "---";

    if (data.connectivity_metric != null) {
      const baseline = data.baseline_connectivity_metric;
      if (baseline > 0) {
        const pct = ((baseline - data.connectivity_metric) / baseline) * 100;
        const sign = pct >= 0 ? "" : "+";
        this.metricEl.textContent =
          `${data.connectivity_metric.toLocaleString()} (${sign}${(-pct).toFixed(1)}% vs base)`;
      } else {
        this.metricEl.textContent = data.connectivity_metric.toLocaleString();
      }
    } else {
      this.metricEl.textContent = "---";
    }

    // ── Galaxy SVG ──
    if (!data.galaxy_view) {
      (this.chartG.node() as SVGGElement).innerHTML = "";
      return;
    }
    this.renderGalaxy(data.galaxy_view);
  }

  private renderGalaxy(g: GalaxyView) {
    this.svg.attr("viewBox", `0 0 ${g.width} ${g.height}`);

    const haloR = g.cluster_r * 1.35;
    const showLabels = g.shown_partitions.length === g.centroids.length;

    let html = "";

    // ── Halos (behind nodes) ──
    html += `<g class="hg-halos">`;
    for (let s = 0; s < g.centroids.length; s++) {
      const [cx, cy] = g.centroids[s];
      const color = PALETTE[s % PALETTE.length];
      const violation = g.over_cap[s];
      // Faint coloured halo so each cluster has a soft background "atmosphere".
      html += `<circle cx="${cx}" cy="${cy}" r="${haloR}" `
        + `fill="${color}" fill-opacity="0.07" stroke="none"/>`;
      // Violation ring: pulses red around partitions over Wmax. Static halo
      // for compliant partitions; no extra ring.
      if (violation) {
        html += `<circle class="hg-halo--violation" cx="${cx}" cy="${cy}" r="${haloR + 4}" `
          + `fill="none" stroke="${VIOLATION_COLOR}" stroke-width="1.6" stroke-dasharray="3 4"/>`;
      }
    }
    html += `</g>`;

    // ── Cut hyperedges (between nodes, behind dots) ──
    html += `<g class="hg-cuts">`;
    for (const edge of g.cut_edges) {
      if (edge.length < 2) continue;
      const pts = edge.map((idx) => {
        const n = g.nodes[idx];
        return n ? [n[1], n[2]] : null;
      }).filter(Boolean) as number[][];
      if (pts.length < 2) continue;
      // 2-node edges: gentle Bézier curve so overlapping straight lines don't
      // collapse into a single visual mass.
      if (pts.length === 2) {
        const [x1, y1] = pts[0];
        const [x2, y2] = pts[1];
        const mx = (x1 + x2) / 2;
        const my = (y1 + y2) / 2;
        const dx = x2 - x1;
        const dy = y2 - y1;
        const norm = Math.hypot(dx, dy) || 1;
        const off = 0.18 * norm;
        const cx = mx + (-dy / norm) * off;
        const cy = my + (dx / norm) * off;
        html += `<path d="M ${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}" `
          + `stroke="${CUT_COLOR}" stroke-width="0.7" stroke-opacity="0.35" fill="none"/>`;
      } else {
        // Multi-node hyperedges (≥3): poly-line through members. Synthetic
        // data only emits 2-node cuts today, so this branch is future-proofing.
        const d = pts.map(([x, y], i) => `${i === 0 ? "M" : "L"} ${x} ${y}`).join(" ");
        html += `<path d="${d}" stroke="${CUT_COLOR}" stroke-width="0.7" `
          + `stroke-opacity="0.30" fill="none"/>`;
      }
    }
    html += `</g>`;

    // ── Nodes (top) ──
    // Group circles by slot so we set fill once via group-level attribute,
    // saving ~2k inline `fill=` strings.
    const bySlot: number[][][] = Array.from({ length: PALETTE.length }, () => []);
    for (const n of g.nodes) {
      const slot = n[0];
      bySlot[slot]?.push([n[1], n[2]]);
    }
    html += `<g class="hg-nodes">`;
    for (let s = 0; s < bySlot.length; s++) {
      const pts = bySlot[s];
      if (!pts || pts.length === 0) continue;
      html += `<g fill="${PALETTE[s % PALETTE.length]}">`;
      for (const [x, y] of pts) {
        html += `<circle cx="${x}" cy="${y}" r="1.6" fill-opacity="0.8"/>`;
      }
      html += `</g>`;
    }
    html += `</g>`;

    // ── Centroid labels ──
    // Title goes radially outward from the ellipse centre; count always sits
    // BELOW the title (closer to the cluster) so the count never escapes the
    // viewBox top for above-centre clusters, while staying readable as
    // "title, then count" left-to-right top-to-bottom for every cluster.
    if (showLabels) {
      const midY = g.height / 2;
      html += `<g class="hg-labels">`;
      for (let s = 0; s < g.centroids.length; s++) {
        const [cx, cy] = g.centroids[s];
        const label = `p${g.shown_partitions[s]}`;
        const sublabel = `${g.shown_sizes[s].toLocaleString()}`;
        const aboveCentre = cy < midY;
        const labelY = aboveCentre ? cy - haloR - 10 : cy + haloR + 12;
        const subY = labelY + 12;
        html += `<text x="${cx}" y="${labelY}" text-anchor="middle" `
          + `font-size="12" font-family="var(--ui)" fill="var(--text-bright)">${label}</text>`;
        html += `<text x="${cx}" y="${subY}" text-anchor="middle" `
          + `font-size="10" font-family="var(--ui)" fill="var(--text-dim)">${sublabel}</text>`;
      }
      html += `</g>`;
    }

    const chartNode = this.chartG.node() as SVGGElement;
    chartNode.innerHTML = html;

    // Fade-in: trigger the CSS transition by toggling a class. Re-running on
    // every render gives a 200ms "settling" feel when instances rotate, no
    // animation engine needed.
    chartNode.classList.remove("hg-fade-in");
    // Force reflow so re-adding the class restarts the animation.
    void chartNode.getBoundingClientRect();
    chartNode.classList.add("hg-fade-in");
  }
}
