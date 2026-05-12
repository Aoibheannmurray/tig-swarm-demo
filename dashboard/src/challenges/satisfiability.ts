import { select, type Selection } from "d3-selection";
import { DisplayPanelBase } from "./base";

interface SatData {
  num_variables: number;
  num_clauses: number;
  num_satisfied: number;
  viz_count: number;     // length of assignment_bits (sub-sampled if num_variables > viz_count)
  viz_stride: number;    // sample step over the full assignment
  assignment_bits: string; // string of "0"/"1", length viz_count
}

type AllSatData = Record<string, SatData>;

const VB_W = 1000;
const VB_H = 1000;
const BANNER_H = 200;            // PASS/FAIL banner across the top
const BANNER_GAP = 20;            // gap between banner and grid
const GRID_TOP = BANNER_H + BANNER_GAP;

// SAT is binary: every clause must be satisfied for the instance to PASS.
// Banner accent colors match the existing palette — dark forest for the
// "satisfied" bucket from the prior histogram, dark wine for danger.
const PASS_COLOR = "#4A7C5A";
const FAIL_COLOR = "#8B2D2D";
// Variable-assignment grid TRUE-cell color, branched on pass/fail so the
// pass state reads through to the secondary view.
const TRUE_PASS = "#4A7C5A";
const TRUE_FAIL = "#4E6B85";

export class SatPanel extends DisplayPanelBase<AllSatData> {
  protected idPrefix = "sat";

  private svg!: Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private bannerG!: Selection<SVGGElement, unknown, HTMLElement, any>;
  private gridG!: Selection<SVGGElement, unknown, HTMLElement, any>;

  private satEl!: HTMLElement;
  private satLabelEl!: HTMLElement;
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
          <div class="solution-sub-label" id="sat-sat-label">CLAUSES</div>
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
    this.satLabelEl = document.getElementById("sat-sat-label")!;
    this.varsEl = document.getElementById("sat-vars")!;
    this.instanceLabelEl = document.getElementById("sat-instance-label")!;
    this.navEl = document.getElementById("sat-nav")!;
    this.agentNameEl = document.getElementById("sat-agent-name")!;
    this.historyNavEl = document.getElementById("sat-history-nav")!;
    this.historyLabelEl = document.getElementById("sat-history-label")!;
    this.historyLiveBtnEl = document.getElementById("sat-hist-live")!;
    this.emptyStateEl = document.getElementById("sat-empty-state")!;

    this.svg = select("#sat-svg") as any;
    this.svg
      .attr("viewBox", `0 0 ${VB_W} ${VB_H}`)
      .attr("preserveAspectRatio", "xMidYMid meet");

    this.bannerG = this.svg.append("g") as any;
    this.gridG = this.svg.append("g")
      .attr("transform", `translate(0,${GRID_TOP})`) as any;

    const wrap = document.getElementById("sat-svg-wrap")!;
    const resize = () => {
      const size = Math.max(0, Math.min(wrap.clientWidth, wrap.clientHeight));
      this.svg.attr("width", size).attr("height", size);
    };
    this.observeResize(wrap, resize);
    resize();
  }

  protected onReset(): void {
    (this.bannerG.node() as SVGGElement).innerHTML = "";
    (this.gridG.node() as SVGGElement).innerHTML = "";
    this.satEl.textContent = "---";
    this.satLabelEl.textContent = "CLAUSES";
    this.varsEl.textContent = "---";
  }

  protected showInstance(data: SatData) {
    if (!data || !data.assignment_bits) {
      (this.bannerG.node() as SVGGElement).innerHTML = "";
      (this.gridG.node() as SVGGElement).innerHTML = "";
      this.satEl.textContent = "---";
      this.satLabelEl.textContent = "CLAUSES";
      this.varsEl.textContent = "---";
      return;
    }

    const m = data.num_clauses;
    const pass = m > 0 && data.num_satisfied === m;
    const unsat = m - data.num_satisfied;

    // PASS/FAIL banner. Solid colored rect across the top with stacked
    // headline + sub-line in white. Same SVG-string-buffer pattern as the
    // grid below — one assignment, no per-element layout churn.
    const bannerColor = pass ? PASS_COLOR : FAIL_COLOR;
    const headline = pass ? "✓ SATISFIED" : "✗ UNSAT";
    const subline = pass
      ? `all ${m.toLocaleString()} clauses satisfied`
      : `${unsat.toLocaleString()} of ${m.toLocaleString()} clauses unsatisfied`;
    const cx = VB_W / 2;
    const headlineY = BANNER_H * 0.50;
    const sublineY = BANNER_H * 0.78;
    const bannerHtml =
      `<rect x="0" y="0" width="${VB_W}" height="${BANNER_H}" fill="${bannerColor}"/>` +
      `<text x="${cx}" y="${headlineY.toFixed(2)}" text-anchor="middle" ` +
        `dominant-baseline="central" fill="rgba(255,255,255,0.96)" ` +
        `font-size="76" font-weight="700" ` +
        `font-family="'JetBrains Mono', monospace" letter-spacing="2">${headline}</text>` +
      `<text x="${cx}" y="${sublineY.toFixed(2)}" text-anchor="middle" ` +
        `dominant-baseline="central" fill="rgba(255,255,255,0.78)" ` +
        `font-size="26" font-family="'JetBrains Mono', monospace">${subline}</text>`;
    (this.bannerG.node() as SVGGElement).innerHTML = bannerHtml;

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
      const trueColor = pass ? TRUE_PASS : TRUE_FAIL;
      const falseColor = "rgba(26,26,26,0.06)";
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

    // Stat box. Label flips PASS↔FAIL to match the banner's headline;
    // dropped the percentage because 99.9%-of-clauses is still UNSAT and
    // the percent reading misleads.
    if (pass) {
      this.satLabelEl.textContent = "SATISFIED";
      this.satEl.textContent = `${m.toLocaleString()} / ${m.toLocaleString()}`;
    } else {
      this.satLabelEl.textContent = "UNSATISFIED";
      this.satEl.textContent = `${unsat.toLocaleString()} / ${m.toLocaleString()}`;
    }
    const sampledNote = data.viz_stride > 1
      ? ` (showing 1/${data.viz_stride})`
      : "";
    this.varsEl.textContent = `${data.num_variables.toLocaleString()}${sampledNote}`;
  }
}
