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

const MARGIN = { top: 8, right: 8, bottom: 8, left: 8 };
const VB_W = 1000;
const VB_H = 1000;
const CHART_W = VB_W - MARGIN.left - MARGIN.right;
const CHART_H = VB_H - MARGIN.top - MARGIN.bottom;

// Olive opacity ramp — strongest cells reach full saturation against the
// cream surface so the heaviest interactions stay unmistakable at K=50.
const HEAT_HUE = "107, 127, 78"; // #6B7F4E
const OPACITY_LOW = 0.08;
const OPACITY_HIGH = 1.0;

// Below this K, draw item-ID labels between the marginal sidebar and the
// matrix so each row/column is identifiable.
const AXIS_LABEL_K_THRESHOLD = 50;

// Top strip reserved for the budget bar.
const BUDGET_H = 40;
const BUDGET_GAP = 18;
const BUDGET_LABEL_W = 80;

// Left strip reserved for the marginal-contribution bars.
const SIDEBAR_W = 120;
const SIDEBAR_GAP = 6;
const ROW_LABEL_W = 26;

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

  private svg!: Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: Selection<SVGGElement, unknown, HTMLElement, any>;

  private valueEl!: HTMLElement;
  private itemsEl!: HTMLElement;
  private weightEl!: HTMLElement;
  private legendMinEl!: HTMLElement;
  private legendMaxEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner knapsack-panel">
        <div class="panel-label">SELECTED · ITEM INTERACTIONS</div>
        <div class="knapsack-agent-name" id="knapsack-agent-name"></div>
        ${this.navsScaffold()}
        <div class="knapsack-svg-wrap" id="knapsack-svg-wrap">
          <svg id="knapsack-svg"></svg>
          <div class="solution-empty-state" id="knapsack-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="knapsack-value-box">
          <div class="solution-sub-label">VALUE</div>
          <div class="solution-sub-value" id="knapsack-value">---</div>
        </div>
        <div class="knapsack-items-box">
          <div class="solution-sub-label">ITEMS</div>
          <div class="solution-sub-value" id="knapsack-items">---</div>
        </div>
        <div class="knapsack-weight-box">
          <div class="solution-sub-label">WEIGHT</div>
          <div class="solution-sub-value" id="knapsack-weight">---</div>
        </div>
        <div class="solution-score">
          <div class="solution-score-label">SCORE</div>
          <div class="solution-score-value" id="knapsack-score">---</div>
          <div class="solution-score-delta" id="knapsack-score-delta"></div>
        </div>
        <div class="kn-legend" aria-hidden="true">
          <span class="kn-legend-end" id="knapsack-legend-min">0</span>
          <div class="kn-legend-swatches">
            <span style="background: linear-gradient(to right, rgba(107,127,78,0.08), rgba(107,127,78,1));"></span>
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
    this.weightEl = document.getElementById("knapsack-weight")!;
    this.legendMinEl = document.getElementById("knapsack-legend-min")!;
    this.legendMaxEl = document.getElementById("knapsack-legend-max")!;
    this.instanceLabelEl = document.getElementById("knapsack-instance-label")!;
    this.navEl = document.getElementById("knapsack-nav")!;
    this.agentNameEl = document.getElementById("knapsack-agent-name")!;
    this.historyNavEl = document.getElementById("knapsack-history-nav")!;
    this.historyLabelEl = document.getElementById("knapsack-history-label")!;
    this.historyLiveBtnEl = document.getElementById("knapsack-hist-live")!;
    this.emptyStateEl = document.getElementById("knapsack-empty-state")!;

    this.svg = select("#knapsack-svg") as any;
    this.svg
      .attr("viewBox", `0 0 ${VB_W} ${VB_H}`)
      .attr("preserveAspectRatio", "xMidYMid meet");

    this.chartG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top})`) as any;

    const wrap = document.getElementById("knapsack-svg-wrap")!;
    const resize = () => {
      const size = Math.max(0, Math.min(wrap.clientWidth, wrap.clientHeight));
      this.svg.attr("width", size).attr("height", size);
    };
    this.observeResize(wrap, resize);
    resize();
  }

  protected onReset(): void {
    (this.chartG.node() as SVGGElement).innerHTML = "";
    this.valueEl.textContent = "---";
    this.itemsEl.textContent = "---";
    this.weightEl.textContent = "---";
  }

  protected showInstance(data: KnapsackData) {
    const chartNode = this.chartG.node() as SVGGElement;

    if (!data || !data.interaction_values || !data.interaction_values.length) {
      chartNode.innerHTML = "";
      this.valueEl.textContent = "---";
      this.itemsEl.textContent = "---";
      this.weightEl.textContent = "---";
      return;
    }

    const k = data.viz_items.length;
    const showAxisLabels = k <= AXIS_LABEL_K_THRESHOLD;

    // Reorder rows/cols via greedy nearest-neighbor TSP on interactions.
    // Permute every per-item array consistently so the budget bar, marginal
    // sidebar and matrix all share the same ordering.
    const order = clusterOrder(data.interaction_values);
    const items = order.map(i => data.viz_items[i]);
    const weights = order.map(i => data.viz_weights?.[i] ?? 0);
    const marginals = order.map(i => data.viz_marginals?.[i] ?? 0);
    const mat: number[][] = order.map(i => order.map(j => data.interaction_values[i][j]));

    // Compute the matrix value range over the upper triangle (the matrix is
    // symmetric; the diagonal is zero by construction).
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

    const parts: string[] = [];

    // ── Geometry ──
    const bodyTop = BUDGET_H + BUDGET_GAP;
    const bodyH = CHART_H - bodyTop;
    const labelW = showAxisLabels ? ROW_LABEL_W : 0;
    const matrixSide = Math.min(
      bodyH,
      CHART_W - SIDEBAR_W - SIDEBAR_GAP - labelW - SIDEBAR_GAP,
    );
    const matrixX = SIDEBAR_W + SIDEBAR_GAP + labelW + SIDEBAR_GAP;
    const matrixY = bodyTop + (bodyH - matrixSide) / 2;
    const cellSize = matrixSide / k;

    // ── Budget bar ──
    // Stacked horizontal bar of weights (one segment per viz item, in cluster
    // order, opacity-coded by marginal contribution). Background tracks
    // max_weight so headroom is visible; a tick marks total_weight.
    const barX = BUDGET_LABEL_W;
    const barY = 12;
    const barH = 18;
    const barW = CHART_W - barX;
    const maxW = Math.max(1, data.max_weight);
    const wToPx = barW / maxW;
    const vizWeightSum = weights.reduce((a, b) => a + b, 0);
    const tailWeight = Math.max(0, data.total_weight - vizWeightSum);
    const maxMarginalForColor = Math.max(1, ...marginals);

    // Caption to the left of the bar — terse, paired with the WEIGHT box
    // below the SVG that carries the actual numbers.
    parts.push(
      `<text x="0" y="${(barY + barH / 2 + 3.5).toFixed(1)}" ` +
      `fill="rgba(26,26,26,0.55)" font-family="var(--mono)" font-size="11" ` +
      `letter-spacing="0.15em">BUDGET</text>`,
    );

    // Background bar (max_weight extent)
    parts.push(
      `<rect x="${barX}" y="${barY}" width="${barW}" height="${barH}" ` +
      `fill="rgba(26,26,26,0.04)" stroke="rgba(26,26,26,0.18)" stroke-width="0.5" rx="2"/>`,
    );

    // Per-item segments (filled portion)
    let segX = barX;
    for (let i = 0; i < k; i++) {
      const w = weights[i] * wToPx;
      if (w <= 0) continue;
      const op = OPACITY_LOW + (OPACITY_HIGH - OPACITY_LOW) *
        (marginals[i] / maxMarginalForColor);
      parts.push(
        `<rect x="${segX.toFixed(3)}" y="${barY}" width="${w.toFixed(3)}" ` +
        `height="${barH}" fill="rgba(${HEAT_HUE}, ${op.toFixed(3)})">` +
        `<title>item ${items[i]}: weight ${weights[i]}, marginal ${marginals[i].toFixed(0)}</title>` +
        `</rect>`,
      );
      segX += w;
    }
    // Tail segment for items beyond the viz cap (when num_selected > k)
    if (tailWeight > 0) {
      const w = tailWeight * wToPx;
      parts.push(
        `<rect x="${segX.toFixed(3)}" y="${barY}" width="${w.toFixed(3)}" ` +
        `height="${barH}" fill="rgba(${HEAT_HUE}, 0.35)" ` +
        `stroke="rgba(${HEAT_HUE},0.7)" stroke-dasharray="2 2" stroke-width="0.5">` +
        `<title>+ ${data.num_selected - k} more items (weight ${tailWeight})</title>` +
        `</rect>`,
      );
      segX += w;
    }

    // ── Marginal sidebar ──
    // Horizontal bars to the left of each matrix row, growing leftward from
    // the matrix's left edge so the bar tips align and length comparisons
    // are easy.
    const maxMarginal = Math.max(1, ...marginals);
    const sidebarRight = matrixX - labelW - SIDEBAR_GAP;
    const sidebarLeft = sidebarRight - SIDEBAR_W;

    // Sidebar caption (top-left, above the first bar)
    parts.push(
      `<text x="${sidebarLeft}" y="${bodyTop - 4}" fill="rgba(26,26,26,0.55)" ` +
      `font-family="var(--mono)" font-size="10" letter-spacing="0.15em">CONTRIBUTION</text>`,
    );

    const barRowH = Math.min(cellSize - 1, cellSize * 0.78);
    const barRowOffset = (cellSize - barRowH) / 2;
    for (let i = 0; i < k; i++) {
      const m = marginals[i];
      const w = (m / maxMarginal) * SIDEBAR_W;
      const yTop = matrixY + i * cellSize + barRowOffset;
      const xLeft = sidebarRight - w;
      const op = OPACITY_LOW + (OPACITY_HIGH - OPACITY_LOW) * (m / maxMarginal);
      parts.push(
        `<rect x="${xLeft.toFixed(3)}" y="${yTop.toFixed(3)}" ` +
        `width="${w.toFixed(3)}" height="${barRowH.toFixed(3)}" ` +
        `fill="rgba(${HEAT_HUE}, ${op.toFixed(3)})">` +
        `<title>item ${items[i]}: contribution ${m.toFixed(0)}</title>` +
        `</rect>`,
      );
    }

    // ── Row labels (between sidebar and matrix) ──
    if (showAxisLabels) {
      const labelFs = Math.max(8, Math.min(11, cellSize * 0.42)).toFixed(1);
      for (let i = 0; i < k; i++) {
        const cy = matrixY + i * cellSize + cellSize / 2;
        parts.push(
          `<text x="${(matrixX - SIDEBAR_GAP).toFixed(1)}" y="${cy.toFixed(1)}" ` +
          `text-anchor="end" dominant-baseline="central" fill="rgba(26,26,26,0.55)" ` +
          `font-family="var(--mono)" font-size="${labelFs}">${items[i]}</text>`,
        );
      }
    }

    // ── Matrix ──
    const gap = Math.min(1, cellSize * 0.06);
    const w = (cellSize - gap).toFixed(3);
    for (let i = 0; i < k; i++) {
      const yPos = (matrixY + i * cellSize).toFixed(3);
      const rowVals = mat[i];
      const rowItem = items[i];
      for (let j = 0; j < k; j++) {
        const xPos = (matrixX + j * cellSize).toFixed(3);
        const v = rowVals[j];
        const fill = i === j
          ? "rgba(26,26,26,0.06)"
          : v <= 0
            ? `rgba(${HEAT_HUE}, 0.04)`
            : `rgba(${HEAT_HUE}, ${opacityScale(v).toFixed(3)})`;
        const colItem = items[j];
        parts.push(
          `<rect x="${xPos}" y="${yPos}" width="${w}" height="${w}" fill="${fill}">` +
          `<title>item ${rowItem} ↔ item ${colItem}: ${v.toFixed(0)}</title>` +
          `</rect>`,
        );
      }
    }
    chartNode.innerHTML = parts.join("");

    this.valueEl.textContent = data.total_value.toLocaleString();
    const suffix = data.num_selected > k ? ` (showing ${k})` : "";
    this.itemsEl.textContent = `${data.num_selected} / ${data.num_items}${suffix}`;
    this.weightEl.textContent = `${data.total_weight} / ${data.max_weight}`;

    if (this.legendMinEl) this.legendMinEl.textContent = minVal.toFixed(0);
    if (this.legendMaxEl) this.legendMaxEl.textContent = maxVal.toFixed(0);
  }
}
