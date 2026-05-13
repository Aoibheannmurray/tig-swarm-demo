import { scaleLinear } from "d3-scale";
import { select, type Selection } from "d3-selection";
import { DisplayPanelBase } from "./base";

interface KnapsackData {
  num_selected: number;
  num_items: number;
  viz_items: number[];
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

// Single-hue opacity ramp on olive. Weakest cells nearly fade
// into the cream surface; strongest cells reach full saturation. Using opacity
// instead of categorical bins makes the highest-value cells unmistakable even
// at K=50 where each cell is only a few pixels wide.
const HEAT_HUE = "107, 127, 78"; // #6B7F4E olive as rgb triplet
const OPACITY_LOW = 0.08;
const OPACITY_HIGH = 1.0;

// Axis labels become unreadable past this K. Below it we render item IDs on
// the top and left margins so the user can identify each row/column.
const AXIS_LABEL_K_THRESHOLD = 50;

export class KnapsackPanel extends DisplayPanelBase<AllKnapsackData> {
  protected idPrefix = "knapsack";

  private svg!: Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: Selection<SVGGElement, unknown, HTMLElement, any>;

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
  }

  protected showInstance(data: KnapsackData) {
    const chartNode = this.chartG.node() as SVGGElement;

    if (!data || !data.interaction_values || !data.interaction_values.length) {
      chartNode.innerHTML = "";
      this.valueEl.textContent = "---";
      this.itemsEl.textContent = "---";
      return;
    }

    const k = data.viz_items.length;
    const showAxisLabels = k <= AXIS_LABEL_K_THRESHOLD;
    // Reserve a margin on the top and left for item-ID labels when K is
    // small enough to render them. The matrix shrinks slightly to make room.
    const labelMargin = showAxisLabels ? Math.min(40, CHART_W * 0.06) : 0;
    const gridW = CHART_W - labelMargin;
    const gridH = CHART_H - labelMargin;
    const cellSize = Math.min(gridW, gridH) / k;

    // Determine the value range across the upper triangle (matrix is
    // symmetric; the diagonal is zero by construction).
    let minVal = Infinity;
    let maxVal = -Infinity;
    for (let i = 0; i < k; i++) {
      for (let j = i + 1; j < k; j++) {
        const v = data.interaction_values[i][j];
        if (v < minVal) minVal = v;
        if (v > maxVal) maxVal = v;
      }
    }
    if (!isFinite(minVal) || minVal === maxVal) {
      minVal = 0;
      maxVal = 1;
    }

    // Continuous opacity scale: maps the value range to [OPACITY_LOW, OPACITY_HIGH].
    // The eye reads opacity as intensity, so the strongest cells stand out
    // sharply against the cream surface even at small cell sizes.
    const opacityScale = scaleLinear()
      .domain([minVal, maxVal])
      .range([OPACITY_LOW, OPACITY_HIGH])
      .clamp(true);

    // Hairline gap between cells — visible at low k, still holds at k=200
    // where cells are ~5px. Caps at 1px so big cells don't leak too much.
    const gap = Math.min(1, cellSize * 0.06);
    const w = (cellSize - gap).toFixed(3);
    const parts: string[] = [];

    // Optional row/column labels. Item IDs from viz_items[i] go on the top
    // (column header) and left (row header) margins.
    if (showAxisLabels) {
      const labelFs = Math.max(8, Math.min(11, cellSize * 0.42)).toFixed(1);
      for (let i = 0; i < k; i++) {
        const itemId = data.viz_items[i];
        // Top column header
        const cx = labelMargin + i * cellSize + cellSize / 2;
        parts.push(
          `<text x="${cx.toFixed(1)}" y="${(labelMargin - 4).toFixed(1)}" ` +
          `text-anchor="middle" fill="rgba(26,26,26,0.55)" ` +
          `font-family="var(--mono)" font-size="${labelFs}">${itemId}</text>`,
        );
        // Left row header
        const cy = labelMargin + i * cellSize + cellSize / 2;
        parts.push(
          `<text x="${(labelMargin - 4).toFixed(1)}" y="${cy.toFixed(1)}" ` +
          `text-anchor="end" dominant-baseline="central" fill="rgba(26,26,26,0.55)" ` +
          `font-family="var(--mono)" font-size="${labelFs}">${itemId}</text>`,
        );
      }
    }

    // Cell grid
    for (let i = 0; i < k; i++) {
      const yPos = (labelMargin + i * cellSize).toFixed(3);
      const rowVals = data.interaction_values[i];
      const rowItem = data.viz_items[i];
      for (let j = 0; j < k; j++) {
        const xPos = (labelMargin + j * cellSize).toFixed(3);
        const v = rowVals[j];
        const fill = i === j
          ? "rgba(26,26,26,0.06)"
          : v <= 0
            // Explicit zero — barely visible terracotta tint.
            ? `rgba(${HEAT_HUE}, 0.04)`
            : `rgba(${HEAT_HUE}, ${opacityScale(v).toFixed(3)})`;
        const colItem = data.viz_items[j];
        parts.push(
          `<rect x="${xPos}" y="${yPos}" width="${w}" height="${w}" fill="${fill}">` +
          `<title>item ${rowItem} ↔ item ${colItem}: ${v.toFixed(2)}</title>` +
          `</rect>`,
        );
      }
    }
    chartNode.innerHTML = parts.join("");

    this.valueEl.textContent = data.total_value.toLocaleString();
    const suffix = data.num_selected > k ? ` (showing ${k})` : "";
    this.itemsEl.textContent = `${data.num_selected} / ${data.num_items}${suffix}`;

    // Legend endpoint labels track the current instance's range.
    if (this.legendMinEl) {
      this.legendMinEl.textContent = minVal.toFixed(1);
    }
    if (this.legendMaxEl) {
      this.legendMaxEl.textContent = maxVal.toFixed(1);
    }
  }
}
