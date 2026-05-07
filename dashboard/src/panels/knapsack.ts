import * as d3 from "d3";
import { DisplayPanelBase } from "./displayPanelBase";

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

// 8-bin diverging palette from the earthen design tokens. Cool hues for
// negative interactions (anti-synergies), warm hues for positive (synergies),
// cream-tinted zero. Order: most-negative → near-zero → near-zero → most-pos.
const NEG_BINS = ["#4E6B85", "#4A8C8A", "#7A4F6E", "#8B6B8C"];
const POS_BINS = ["#6B7F4E", "#A66E45", "#C68F3E", "#B8541F"];

export class KnapsackPanel extends DisplayPanelBase<AllKnapsackData> {
  protected idPrefix = "knapsack";

  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;

  private valueEl!: HTMLElement;
  private itemsEl!: HTMLElement;
  private legendMinEl!: HTMLElement;
  private legendMaxEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner knapsack-panel">
        <div class="panel-label">INTERACTIONS</div>
        <div class="knapsack-agent-name" id="knapsack-agent-name"></div>
        <div class="solution-history-nav" id="knapsack-history-nav" style="display:none">
          <button class="solution-nav-btn" id="knapsack-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="solution-history-label" id="knapsack-history-label"></span>
          <button class="solution-nav-btn" id="knapsack-hist-next" title="Next global best">&rsaquo;</button>
          <button class="solution-history-live" id="knapsack-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="solution-nav" id="knapsack-nav" style="display:none">
          <button class="solution-nav-btn" id="knapsack-prev">&lsaquo;</button>
          <span class="solution-instance-label" id="knapsack-instance-label"></span>
          <button class="solution-nav-btn" id="knapsack-next">&rsaquo;</button>
        </div>
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
          <span class="kn-legend-end" id="knapsack-legend-min">−</span>
          <div class="kn-legend-swatches">
            <span style="background:#4E6B85"></span>
            <span style="background:#4A8C8A"></span>
            <span style="background:#7A4F6E"></span>
            <span style="background:#8B6B8C"></span>
            <span class="kn-legend-zero"></span>
            <span style="background:#6B7F4E"></span>
            <span style="background:#A66E45"></span>
            <span style="background:#C68F3E"></span>
            <span style="background:#B8541F"></span>
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

    this.svg = d3.select("#knapsack-svg") as any;
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
    new ResizeObserver(resize).observe(wrap);
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
    const cellSize = Math.min(CHART_W, CHART_H) / k;

    // Compute the negative and positive halves of the value range separately
    // so an asymmetric outlier on one side doesn't compress the other half's
    // perceptual resolution. Diagonal entries are zeroed server-side so they
    // never set the bounds.
    let minNeg = 0;
    let maxPos = 0;
    for (let i = 0; i < k; i++) {
      for (let j = i + 1; j < k; j++) {
        const v = data.interaction_values[i][j];
        if (v < minNeg) minNeg = v;
        if (v > maxPos) maxPos = v;
      }
    }

    // Two quantize ramps: negative values map to 4 cool hues, positive to 4
    // warm hues. d3 handles the bucket boundaries (equal-width within each
    // half). When one half is empty the corresponding scale is unused.
    const negScale = d3.scaleQuantize<string>()
      .domain([minNeg, 0])
      .range(NEG_BINS);
    const posScale = d3.scaleQuantize<string>()
      .domain([0, maxPos])
      .range(POS_BINS);

    // Hairline gap between cells — visible at low k, still holds at k=200
    // where cells are ~5px. Caps at 1px so big cells don't leak too much.
    const gap = Math.min(1, cellSize * 0.06);
    const w = (cellSize - gap).toFixed(3);
    const cells: string[] = [];
    for (let i = 0; i < k; i++) {
      const yPos = (i * cellSize).toFixed(3);
      const rowVals = data.interaction_values[i];
      for (let j = 0; j < k; j++) {
        const xPos = (j * cellSize).toFixed(3);
        const v = rowVals[j];
        const fill = i === j
          ? "rgba(26,26,26,0.06)"
          : v === 0
            ? "rgba(26,26,26,0.03)"
            : v < 0 ? negScale(v) : posScale(v);
        cells.push(
          `<rect x="${xPos}" y="${yPos}" width="${w}" height="${w}" fill="${fill}">` +
          `<title>${i} ↔ ${j}: ${v.toFixed(2)}</title>` +
          `</rect>`,
        );
      }
    }
    chartNode.innerHTML = cells.join("");

    this.valueEl.textContent = data.total_value.toLocaleString();
    const suffix = data.num_selected > k ? ` (showing ${k})` : "";
    this.itemsEl.textContent = `${data.num_selected} / ${data.num_items}${suffix}`;

    // Legend endpoint labels track the current instance's range. fixed(1)
    // keeps the strip visually balanced regardless of magnitude.
    if (this.legendMinEl) {
      this.legendMinEl.textContent = minNeg < 0 ? minNeg.toFixed(1) : "0";
    }
    if (this.legendMaxEl) {
      this.legendMaxEl.textContent = maxPos > 0 ? `+${maxPos.toFixed(1)}` : "0";
    }
  }
}
