import * as d3 from "d3";
import { DisplayPanelBase } from "./displayPanelBase";

interface HypergraphData {
  num_nodes: number;
  num_parts: number;
  max_part_size: number;
  partition_sizes: number[];
  connectivity_metric: number | null;
  baseline_connectivity_metric: number;
}

type AllHypergraphData = Record<string, HypergraphData>;

const VB_W = 600;
const VB_H = 300;
const MARGIN = { top: 24, right: 16, bottom: 32, left: 48 };

export class HypergraphPanel extends DisplayPanelBase<AllHypergraphData> {
  protected idPrefix = "hg";

  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private nodesEl!: HTMLElement;
  private metricEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner hg-panel">
        <div class="panel-label">PARTITION BALANCE</div>
        <div class="knapsack-agent-name" id="hg-agent-name"></div>
        <div class="solution-history-nav" id="hg-history-nav" style="display:none">
          <button class="solution-nav-btn" id="hg-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="solution-history-label" id="hg-history-label"></span>
          <button class="solution-nav-btn" id="hg-hist-next" title="Next global best">&rsaquo;</button>
          <button class="solution-history-live" id="hg-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="solution-nav" id="hg-nav" style="display:none">
          <button class="solution-nav-btn" id="hg-prev">&lsaquo;</button>
          <span class="solution-instance-label" id="hg-instance-label"></span>
          <button class="solution-nav-btn" id="hg-next">&rsaquo;</button>
        </div>
        <div class="hg-svg-wrap" id="hg-svg-wrap">
          <svg id="hg-svg"></svg>
          <div class="solution-empty-state" id="hg-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="knapsack-value-box">
          <div class="solution-sub-label">NODES</div>
          <div class="solution-sub-value" id="hg-nodes">---</div>
        </div>
        <div class="knapsack-items-box">
          <div class="solution-sub-label">CONNECTIVITY</div>
          <div class="solution-sub-value" id="hg-metric">---</div>
        </div>
        <div class="solution-score">
          <div class="solution-score-label">SCORE</div>
          <div class="solution-score-value" id="hg-score">---</div>
          <div class="solution-score-delta" id="hg-score-delta"></div>
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
    this.metricEl = document.getElementById("hg-metric")!;

    this.svg = d3.select("#hg-svg") as any;
    this.svg
      .attr("viewBox", `0 0 ${VB_W} ${VB_H}`)
      .attr("preserveAspectRatio", "xMidYMid meet");

    this.chartG = this.svg.append("g") as any;

    const wrap = document.getElementById("hg-svg-wrap")!;
    const resize = () => {
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      this.svg.attr("width", w).attr("height", h);
    };
    new ResizeObserver(resize).observe(wrap);
    resize();
  }

  protected onReset(): void {
    (this.chartG.node() as SVGGElement).innerHTML = "";
    this.nodesEl.textContent = "---";
    this.metricEl.textContent = "---";
  }

  protected showInstance(data: HypergraphData) {
    if (!data || !data.partition_sizes || data.partition_sizes.length === 0) {
      this.onReset();
      return;
    }

    const sizes = data.partition_sizes;
    const n = sizes.length;
    const maxSize = data.max_part_size;
    const ideal = data.num_nodes / data.num_parts;

    const iW = VB_W - MARGIN.left - MARGIN.right;
    const iH = VB_H - MARGIN.top - MARGIN.bottom;
    const yMax = Math.max(maxSize * 1.05, Math.max(...sizes) * 1.05);

    const x = d3.scaleBand<number>()
      .domain(d3.range(n))
      .range([0, iW])
      .padding(0.15);

    const y = d3.scaleLinear()
      .domain([0, yMax])
      .range([iH, 0]);

    let html = `<g transform="translate(${MARGIN.left},${MARGIN.top})">`;

    for (let i = 0; i < n; i++) {
      const barH = iH - y(sizes[i]);
      const overMax = sizes[i] > maxSize;
      const fill = overMax ? "var(--danger)" : "var(--color-accent)";
      html += `<rect x="${x(i)}" y="${y(sizes[i])}" width="${x.bandwidth()}" height="${barH}" fill="${fill}" opacity="0.85"/>`;
    }

    const maxY = y(maxSize);
    html += `<line x1="0" x2="${iW}" y1="${maxY}" y2="${maxY}" stroke="var(--danger)" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>`;
    html += `<text x="${iW + 4}" y="${maxY + 3}" fill="var(--danger)" font-size="8" font-family="var(--ui)">max</text>`;

    const idealY = y(ideal);
    html += `<line x1="0" x2="${iW}" y1="${idealY}" y2="${idealY}" stroke="var(--info)" stroke-width="1" stroke-dasharray="2,2" opacity="0.5"/>`;
    html += `<text x="${iW + 4}" y="${idealY + 3}" fill="var(--info)" font-size="8" font-family="var(--ui)">ideal</text>`;

    html += `<line x1="0" x2="${iW}" y1="${iH}" y2="${iH}" stroke="var(--border-default)" stroke-width="0.5"/>`;
    html += `<line x1="0" x2="0" y1="0" y2="${iH}" stroke="var(--border-default)" stroke-width="0.5"/>`;

    const yTicks = y.ticks(5);
    for (const t of yTicks) {
      const ty = y(t);
      html += `<text x="-6" y="${ty + 3}" text-anchor="end" fill="var(--ink-dim)" font-size="8" font-family="var(--ui)">${t}</text>`;
      html += `<line x1="0" x2="${iW}" y1="${ty}" y2="${ty}" stroke="var(--border-subtle)" stroke-width="0.5"/>`;
    }

    html += `<text x="${iW / 2}" y="${iH + 24}" text-anchor="middle" fill="var(--ink-dim)" font-size="9" font-family="var(--ui)">Partition index (${n} parts)</text>`;

    html += `</g>`;
    (this.chartG.node() as SVGGElement).innerHTML = html;

    this.nodesEl.textContent = `${data.num_nodes.toLocaleString()} across ${data.num_parts} parts`;

    if (data.connectivity_metric != null) {
      const baseline = data.baseline_connectivity_metric;
      const pct = baseline > 0
        ? ((baseline - data.connectivity_metric) / baseline * 100).toFixed(2)
        : "---";
      this.metricEl.textContent =
        `${data.connectivity_metric.toLocaleString()} (${pct}% below baseline)`;
    } else {
      this.metricEl.textContent = "---";
    }
  }
}
