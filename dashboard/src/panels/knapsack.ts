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

export class KnapsackPanel extends DisplayPanelBase<AllKnapsackData> {
  protected idPrefix = "knapsack";

  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;

  private valueEl!: HTMLElement;
  private itemsEl!: HTMLElement;

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
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.scoreEl = document.getElementById("knapsack-score")!;
    this.scoreDeltaEl = document.getElementById("knapsack-score-delta")!;
    this.valueEl = document.getElementById("knapsack-value")!;
    this.itemsEl = document.getElementById("knapsack-items")!;
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

    let minVal = Infinity;
    let maxVal = -Infinity;
    for (let i = 0; i < k; i++) {
      for (let j = i + 1; j < k; j++) {
        const v = data.interaction_values[i][j];
        if (v < minVal) minVal = v;
        if (v > maxVal) maxVal = v;
      }
    }
    if (minVal === maxVal) {
      minVal = 0;
      maxVal = 1;
    }

    // Cream → terracotta single-hue ramp in HCL space (perceptually uniform,
    // no blue dominating the grid). Low-interaction cells fade into the cream
    // surface; high-interaction cells read as saturated terracotta.
    const colorScale = d3.scaleSequential(
      d3.interpolateHcl("#F2EDE4", "#B8541F"),
    ).domain([minVal, maxVal]);

    const w = cellSize.toFixed(3);
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
            : colorScale(v);
        cells.push(`<rect x="${xPos}" y="${yPos}" width="${w}" height="${w}" fill="${fill}"/>`);
      }
    }
    chartNode.innerHTML = cells.join("");

    this.valueEl.textContent = data.total_value.toLocaleString();
    const suffix = data.num_selected > k ? ` (showing ${k})` : "";
    this.itemsEl.textContent = `${data.num_selected} / ${data.num_items}${suffix}`;
  }
}
