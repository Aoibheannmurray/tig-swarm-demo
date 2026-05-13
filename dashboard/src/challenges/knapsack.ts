import { scaleLinear } from "d3-scale";
import { select, type Selection } from "d3-selection";
import { DisplayPanelBase } from "./base";

interface KnapsackData {
  num_selected: number;
  num_items: number;
  viz_items: number[];
  viz_weights: number[];
  viz_marginals: number[];
  interaction_values: number[][];
  total_value: number;
  max_weight: number;
  total_weight: number;
}

type AllKnapsackData = Record<string, KnapsackData>;

// Olive opacity ramp — strongest cells reach full saturation against the
// cream surface so the heaviest interactions stay unmistakable.
const HEAT_HUE = "107, 127, 78"; // #6B7F4E
const OPACITY_LOW = 0.18;
const OPACITY_HIGH = 1.0;
// Explicit-zero cells stay a touch fainter than OPACITY_LOW so they're
// visibly distinct from low-but-nonzero values without disappearing.
const OPACITY_ZERO = 0.1;

// Fixed pixel sizes (no viewBox scaling) so each cell stays the same size
// regardless of K — when the matrix is bigger than the panel, the user
// scrolls instead of squinting at sub-pixel cells.
const CELL_SIZE = 6;
const CELL_GAP = 1;

// Left sidebar (marginal-contribution bars + row labels). Stays put while
// the matrix scrolls horizontally so each row is always identifiable.
const SIDEBAR_W = 120;
const SIDEBAR_GAP = 6;
const ROW_LABEL_W = 32;
const SIDEBAR_PAD_TOP = 18; // headroom for the CONTRIBUTION caption

// Greedy nearest-neighbor leaf-ordering on the K×K interaction matrix.
// Seeded with the item that has the largest total interaction (the most
// "central" member of the visible set), then walks to the most-similar
// unvisited item at each step. Block-diagonal team structure becomes
// visible without paying for hierarchical clustering.
function clusterOrder(mat: number[][]): number[] {
  const k = mat.length;
  if (k <= 2) return mat.map((_, i) => i);

  let seed = 0;
  let bestSum = -Infinity;
  for (let i = 0; i < k; i++) {
    let s = 0;
    for (let j = 0; j < k; j++) s += mat[i][j];
    if (s > bestSum) { bestSum = s; seed = i; }
  }

  const order: number[] = [seed];
  const visited = new Uint8Array(k);
  visited[seed] = 1;

  while (order.length < k) {
    const last = order[order.length - 1];
    const row = mat[last];
    let next = -1;
    let bestVal = -Infinity;
    for (let j = 0; j < k; j++) {
      if (visited[j]) continue;
      if (row[j] > bestVal) { bestVal = row[j]; next = j; }
    }
    if (next < 0) {
      for (let j = 0; j < k; j++) if (!visited[j]) { next = j; break; }
    }
    visited[next] = 1;
    order.push(next);
  }

  return order;
}

export class KnapsackPanel extends DisplayPanelBase<AllKnapsackData> {
  protected idPrefix = "knapsack";

  private sidebarSvg!: Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private matrixSvg!: Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private matrixScrollEl!: HTMLElement;

  private valueEl!: HTMLElement;
  private itemsEl!: HTMLElement;
  private legendMinEl!: HTMLElement;
  private legendMaxEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner knapsack-panel">
        <div class="panel-label">SELECTED · ITEM INTERACTIONS</div>
        <div class="knapsack-agent-name" id="knapsack-agent-name"></div>
        ${this.navsScaffold()}
        <div class="knapsack-svg-wrap" id="knapsack-svg-wrap">
          <div class="knapsack-grid">
            <svg id="knapsack-sidebar-svg"></svg>
            <div class="knapsack-matrix-hscroll" id="knapsack-matrix-hscroll">
              <svg id="knapsack-matrix-svg"></svg>
            </div>
          </div>
          <div class="solution-empty-state" id="knapsack-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="knapsack-value-box">
          <div class="solution-sub-label">SELECTED</div>
          <div class="knapsack-selected-value" id="knapsack-items">---</div>
          <div class="solution-sub-label">VALUE</div>
          <div class="solution-sub-value" id="knapsack-value">---</div>
        </div>
        <div class="solution-score">
          <div class="solution-score-label">SCORE</div>
          <div class="solution-score-value" id="knapsack-score">---</div>
          <div class="solution-score-delta" id="knapsack-score-delta"></div>
        </div>
        <div class="kn-legend" aria-hidden="true">
          <span class="kn-legend-end" id="knapsack-legend-min">0</span>
          <div class="kn-legend-swatches">
            <span style="background: linear-gradient(to right, rgba(107,127,78,0.18), rgba(107,127,78,1));"></span>
          </div>
          <span class="kn-legend-end" id="knapsack-legend-max">+</span>
        </div>
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.scoreEl = document.getElementById("knapsack-score")!;
    this.scoreDeltaEl = document.getElementById("knapsack-score-delta")!;
    this.valueEl = document.getElementById("knapsack-value")!;
    this.itemsEl = document.getElementById("knapsack-items")!;
    this.legendMinEl = document.getElementById("knapsack-legend-min")!;
    this.legendMaxEl = document.getElementById("knapsack-legend-max")!;
    this.instanceLabelEl = document.getElementById("knapsack-instance-label")!;
    this.navEl = document.getElementById("knapsack-nav")!;
    this.agentNameEl = document.getElementById("knapsack-agent-name")!;
    this.historyNavEl = document.getElementById("knapsack-history-nav")!;
    this.historyLabelEl = document.getElementById("knapsack-history-label")!;
    this.historyLiveBtnEl = document.getElementById("knapsack-hist-live")!;
    this.emptyStateEl = document.getElementById("knapsack-empty-state")!;

    this.sidebarSvg = select("#knapsack-sidebar-svg") as any;
    this.matrixSvg = select("#knapsack-matrix-svg") as any;
    this.matrixScrollEl = document.getElementById("knapsack-matrix-hscroll")!;
  }

  protected onReset(): void {
    (this.sidebarSvg.node() as SVGSVGElement).innerHTML = "";
    (this.matrixSvg.node() as SVGSVGElement).innerHTML = "";
    this.valueEl.textContent = "---";
    this.itemsEl.textContent = "---";
  }

  protected showInstance(data: KnapsackData) {
    const sidebarNode = this.sidebarSvg.node() as SVGSVGElement;
    const matrixNode = this.matrixSvg.node() as SVGSVGElement;

    if (!data || !data.interaction_values || !data.interaction_values.length) {
      sidebarNode.innerHTML = "";
      matrixNode.innerHTML = "";
      this.valueEl.textContent = "---";
      this.itemsEl.textContent = "---";
      return;
    }

    const k = data.viz_items.length;

    // Reorder rows/cols via greedy nearest-neighbor TSP on interactions.
    // Permute every per-item array consistently so the sidebar bars and the
    // matrix share the same row ordering.
    const order = clusterOrder(data.interaction_values);
    const items = order.map(i => data.viz_items[i]);
    const marginals = order.map(i => data.viz_marginals?.[i] ?? 0);
    const mat: number[][] = order.map(i => order.map(j => data.interaction_values[i][j]));

    // Matrix value range over upper triangle.
    let minVal = Infinity;
    let maxVal = -Infinity;
    for (let i = 0; i < k; i++) {
      for (let j = i + 1; j < k; j++) {
        const v = mat[i][j];
        if (v < minVal) minVal = v;
        if (v > maxVal) maxVal = v;
      }
    }
    if (!isFinite(minVal) || minVal === maxVal) {
      minVal = 0;
      maxVal = 1;
    }
    const opacityScale = scaleLinear()
      .domain([minVal, maxVal])
      .range([OPACITY_LOW, OPACITY_HIGH])
      .clamp(true);

    // ── Intrinsic dimensions ──
    // Drop row labels below ~10 px cells — they'd overlap and look like noise.
    // The cell tooltip still names each item on hover.
    const showRowLabels = CELL_SIZE >= 10;
    const matrixPx = k * CELL_SIZE;
    const sidebarTotalW = SIDEBAR_W + SIDEBAR_GAP +
      (showRowLabels ? ROW_LABEL_W + SIDEBAR_GAP : 0);
    const sidebarH = SIDEBAR_PAD_TOP + matrixPx;
    const matrixH = SIDEBAR_PAD_TOP + matrixPx;

    this.sidebarSvg
      .attr("width", sidebarTotalW)
      .attr("height", sidebarH);
    this.matrixSvg
      .attr("width", matrixPx)
      .attr("height", matrixH);

    // ── Sidebar SVG: caption + marginal bars + row labels ──
    const sParts: string[] = [];
    const maxMarginal = Math.max(1, ...marginals);
    const sidebarRight = SIDEBAR_W; // bars end at x = SIDEBAR_W
    const labelX = SIDEBAR_W + SIDEBAR_GAP + ROW_LABEL_W; // labels right-aligned here

    sParts.push(
      `<text x="0" y="12" fill="rgba(26,26,26,0.55)" ` +
      `font-family="var(--mono)" font-size="10" letter-spacing="0.15em">CONTRIBUTION</text>`,
    );

    const barH = Math.max(2, CELL_SIZE - CELL_GAP * 2);
    const barRowOffset = (CELL_SIZE - barH) / 2;
    const labelFs = Math.max(8, Math.min(11, CELL_SIZE * 0.5)).toFixed(1);

    for (let i = 0; i < k; i++) {
      const m = marginals[i];
      const w = (m / maxMarginal) * SIDEBAR_W;
      const yTop = SIDEBAR_PAD_TOP + i * CELL_SIZE + barRowOffset;
      const xLeft = sidebarRight - w;
      const op = OPACITY_LOW + (OPACITY_HIGH - OPACITY_LOW) * (m / maxMarginal);
      sParts.push(
        `<rect x="${xLeft.toFixed(3)}" y="${yTop.toFixed(3)}" ` +
        `width="${w.toFixed(3)}" height="${barH.toFixed(3)}" ` +
        `fill="rgba(${HEAT_HUE}, ${op.toFixed(3)})">` +
        `<title>item ${items[i]}: contribution ${m.toFixed(0)}</title>` +
        `</rect>`,
      );

      if (showRowLabels) {
        const cy = SIDEBAR_PAD_TOP + i * CELL_SIZE + CELL_SIZE / 2;
        sParts.push(
          `<text x="${labelX}" y="${cy.toFixed(1)}" ` +
          `text-anchor="end" dominant-baseline="central" fill="rgba(26,26,26,0.55)" ` +
          `font-family="var(--mono)" font-size="${labelFs}">${items[i]}</text>`,
        );
      }
    }
    sidebarNode.innerHTML = sParts.join("");

    // ── Matrix SVG ──
    const mParts: string[] = [];
    const cellW = (CELL_SIZE - CELL_GAP).toFixed(3);
    for (let i = 0; i < k; i++) {
      const yPos = (SIDEBAR_PAD_TOP + i * CELL_SIZE).toFixed(3);
      const rowVals = mat[i];
      const rowItem = items[i];
      for (let j = 0; j < k; j++) {
        const xPos = (j * CELL_SIZE).toFixed(3);
        const v = rowVals[j];
        const fill = i === j
          ? "rgba(26,26,26,0.06)"
          : v <= 0
            ? `rgba(${HEAT_HUE}, ${OPACITY_ZERO})`
            : `rgba(${HEAT_HUE}, ${opacityScale(v).toFixed(3)})`;
        const colItem = items[j];
        mParts.push(
          `<rect x="${xPos}" y="${yPos}" width="${cellW}" height="${cellW}" fill="${fill}">` +
          `<title>item ${rowItem} ↔ item ${colItem}: ${v.toFixed(0)}</title>` +
          `</rect>`,
        );
      }
    }
    matrixNode.innerHTML = mParts.join("");

    // Reset horizontal scroll so a new instance starts at column 0.
    this.matrixScrollEl.scrollLeft = 0;

    this.valueEl.textContent = data.total_value.toLocaleString();
    this.itemsEl.textContent = data.num_selected.toLocaleString();

    if (this.legendMinEl) this.legendMinEl.textContent = minVal.toFixed(0);
    if (this.legendMaxEl) this.legendMaxEl.textContent = maxVal.toFixed(0);
  }
}
