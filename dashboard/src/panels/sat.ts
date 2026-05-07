import * as d3 from "d3";
import { DisplayPanelBase } from "./displayPanelBase";

interface SatData {
  num_variables: number;
  num_clauses: number;
  num_satisfied: number;
  viz_count: number;     // length of assignment_bits (sub-sampled if num_variables > viz_count)
  viz_stride: number;    // sample step over the full assignment
  assignment_bits: string; // string of "0"/"1", length viz_count
  clause_bins: number[][]; // 50 bins of [c0, c1, c2, c3]
}

type AllSatData = Record<string, SatData>;

const VB_W = 1000;
const VB_H = 1000;
const HIST_H = 180;             // top strip for clause-satisfaction histogram
const HIST_GAP = 20;             // gap between histogram and grid
const GRID_TOP = HIST_H + HIST_GAP;

// Stacked-bar colors per "satisfying-literal count" bucket.
const BIN_COLORS = ["#d04d4d", "#d8a13a", "#7ec043", "#3a8a3a"]; // 0,1,2,3 sats

export class SatPanel extends DisplayPanelBase<AllSatData> {
  protected idPrefix = "sat";

  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private histG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private gridG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;

  private satEl!: HTMLElement;
  private varsEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner knapsack-panel">
        <div class="panel-label">CLAUSES &amp; ASSIGNMENT</div>
        <div class="knapsack-agent-name" id="sat-agent-name"></div>
        <div class="solution-history-nav" id="sat-history-nav" style="display:none">
          <button class="solution-nav-btn" id="sat-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="solution-history-label" id="sat-history-label"></span>
          <button class="solution-nav-btn" id="sat-hist-next" title="Next global best">&rsaquo;</button>
          <button class="solution-history-live" id="sat-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="solution-nav" id="sat-nav" style="display:none">
          <button class="solution-nav-btn" id="sat-prev">&lsaquo;</button>
          <span class="solution-instance-label" id="sat-instance-label"></span>
          <button class="solution-nav-btn" id="sat-next">&rsaquo;</button>
        </div>
        <div class="sat-svg-wrap" id="sat-svg-wrap">
          <svg id="sat-svg"></svg>
          <div class="solution-empty-state" id="sat-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="knapsack-value-box">
          <div class="solution-sub-label">SATISFIED</div>
          <div class="solution-sub-value" id="sat-sat">---</div>
        </div>
        <div class="knapsack-items-box">
          <div class="solution-sub-label">VARIABLES</div>
          <div class="solution-sub-value" id="sat-vars">---</div>
        </div>
        <div class="solution-score">
          <div class="solution-score-label">SCORE</div>
          <div class="solution-score-value" id="sat-score">---</div>
          <div class="solution-score-delta" id="sat-score-delta"></div>
        </div>
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.scoreEl = document.getElementById("sat-score")!;
    this.scoreDeltaEl = document.getElementById("sat-score-delta")!;
    this.satEl = document.getElementById("sat-sat")!;
    this.varsEl = document.getElementById("sat-vars")!;
    this.instanceLabelEl = document.getElementById("sat-instance-label")!;
    this.navEl = document.getElementById("sat-nav")!;
    this.agentNameEl = document.getElementById("sat-agent-name")!;
    this.historyNavEl = document.getElementById("sat-history-nav")!;
    this.historyLabelEl = document.getElementById("sat-history-label")!;
    this.historyLiveBtnEl = document.getElementById("sat-hist-live")!;
    this.emptyStateEl = document.getElementById("sat-empty-state")!;

    this.svg = d3.select("#sat-svg") as any;
    this.svg
      .attr("viewBox", `0 0 ${VB_W} ${VB_H}`)
      .attr("preserveAspectRatio", "xMidYMid meet");

    this.histG = this.svg.append("g") as any;
    this.gridG = this.svg.append("g")
      .attr("transform", `translate(0,${GRID_TOP})`) as any;

    const wrap = document.getElementById("sat-svg-wrap")!;
    const resize = () => {
      const size = Math.max(0, Math.min(wrap.clientWidth, wrap.clientHeight));
      this.svg.attr("width", size).attr("height", size);
    };
    new ResizeObserver(resize).observe(wrap);
    resize();
  }

  protected onReset(): void {
    (this.histG.node() as SVGGElement).innerHTML = "";
    (this.gridG.node() as SVGGElement).innerHTML = "";
    this.satEl.textContent = "---";
    this.varsEl.textContent = "---";
  }

  protected showInstance(data: SatData) {
    if (!data || !data.assignment_bits || !data.clause_bins) {
      (this.histG.node() as SVGGElement).innerHTML = "";
      (this.gridG.node() as SVGGElement).innerHTML = "";
      this.satEl.textContent = "---";
      this.varsEl.textContent = "---";
      return;
    }

    // Build SVG via a string buffer and assign once. d3.append per
    // element triggers a layout pass each call — for thousands of
    // assignment cells that becomes the dominant cost.
    let histHtml = "";
    const bins = data.clause_bins;
    const numBins = bins.length;
    const binW = VB_W / numBins;
    let maxBinTotal = 1;
    for (const b of bins) {
      const t = b[0] + b[1] + b[2] + b[3];
      if (t > maxBinTotal) maxBinTotal = t;
    }
    const segWidth = (binW - 0.6).toFixed(2);
    for (let bi = 0; bi < numBins; bi++) {
      let yCursor = HIST_H;
      const bin = bins[bi];
      const xPos = (bi * binW).toFixed(2);
      for (let k = 0; k < 4; k++) {
        const segH = (bin[k] / maxBinTotal) * HIST_H;
        if (segH <= 0) continue;
        yCursor -= segH;
        histHtml += `<rect x="${xPos}" y="${yCursor.toFixed(2)}" width="${segWidth}" height="${segH.toFixed(2)}" fill="${BIN_COLORS[k]}"/>`;
      }
    }
    histHtml += `<line x1="0" x2="${VB_W}" y1="${HIST_H + 1}" y2="${HIST_H + 1}" stroke="rgba(255,255,255,0.18)" stroke-width="1"/>`;
    (this.histG.node() as SVGGElement).innerHTML = histHtml;

    // Variable-assignment grid.
    let gridHtml = "";
    const gridH = VB_H - GRID_TOP;
    const n = data.viz_count;
    if (n > 0) {
      const aspect = VB_W / gridH;
      const cols = Math.max(1, Math.round(Math.sqrt(n * aspect)));
      const rows = Math.ceil(n / cols);
      const cellW = VB_W / cols;
      const cellH = gridH / rows;
      const trueColor = "#4a7fd6";
      const falseColor = "rgba(255,255,255,0.06)";
      const bits = data.assignment_bits;
      const w = Math.max(0.5, cellW - 0.4).toFixed(3);
      const h = Math.max(0.5, cellH - 0.4).toFixed(3);

      for (let i = 0; i < n; i++) {
        const r = Math.floor(i / cols);
        const c = i % cols;
        const isTrue = bits.charCodeAt(i) === 49;
        gridHtml += `<rect x="${(c * cellW).toFixed(3)}" y="${(r * cellH).toFixed(3)}" width="${w}" height="${h}" fill="${isTrue ? trueColor : falseColor}"/>`;
      }
    }
    (this.gridG.node() as SVGGElement).innerHTML = gridHtml;

    const satPct = data.num_clauses > 0
      ? (data.num_satisfied / data.num_clauses) * 100
      : 0;
    this.satEl.textContent = `${data.num_satisfied.toLocaleString()} / ${data.num_clauses.toLocaleString()} (${satPct.toFixed(2)}%)`;
    const sampledNote = data.viz_stride > 1
      ? ` (showing 1/${data.viz_stride})`
      : "";
    this.varsEl.textContent = `${data.num_variables.toLocaleString()}${sampledNote}`;
  }
}
