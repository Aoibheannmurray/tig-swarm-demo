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
const BANNER_H = 70;             // small PASS/FAIL banner across the top
const BANNER_GAP = 16;            // gap between banner and grid
const GRID_TOP = BANNER_H + BANNER_GAP;
// Leave empty SVG space below the grid so the absolutely-positioned
// VARIABLES / SATISFIED stat boxes (CSS: bottom:16px) overlay a clean
// background instead of sitting on top of grid cells.
const GRID_BOTTOM_PAD = 130;

// SAT is binary: every clause must be satisfied for the instance to PASS.
// Banner accent colors match the existing palette — dark forest for the
// "satisfied" bucket from the prior histogram, dark wine for danger.
const PASS_COLOR = "#4A7C5A";
const FAIL_COLOR = "#8B2D2D";
// Neutral "scanning" tone — sits between PASS_COLOR and FAIL_COLOR so the
// banner doesn't pre-announce the verdict before the scanline finishes.
const SCAN_COLOR = "#3a4150";
// Variable-assignment grid TRUE-cell color, branched on pass/fail so the
// pass state reads through to the secondary view.
const TRUE_PASS = "#4A7C5A";
const TRUE_FAIL = "#4E6B85";

// Scanline sweep: the grid "reads" the assignment top-to-bottom on each
// render. Short enough that 8-second instance rotations still feel snappy.
const SCAN_DURATION_MS = 800;

export class SatPanel extends DisplayPanelBase<AllSatData> {
  protected idPrefix = "sat";

  private svg!: Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private bannerG!: Selection<SVGGElement, unknown, HTMLElement, any>;
  private gridG!: Selection<SVGGElement, unknown, HTMLElement, any>;

  private satEl!: HTMLElement;
  private satLabelEl!: HTMLElement;
  private varsEl!: HTMLElement;

  // Active scanline animation handle, plus a token that increments on every
  // (re)render so an in-flight rAF callback knows it's been superseded and
  // bails out instead of mutating the next instance's DOM.
  private scanRafId: number | null = null;
  private scanToken = 0;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner knapsack-panel">
        <div class="panel-label">CLAUSES &amp; ASSIGNMENT</div>
        <div class="knapsack-agent-name" id="sat-agent-name"></div>
        ${this.navsScaffold()}
        <div class="sat-svg-wrap" id="sat-svg-wrap">
          <svg id="sat-svg"></svg>
          <div class="solution-empty-state" id="sat-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="knapsack-value-box">
          <div class="solution-sub-label">VARIABLES</div>
          <div class="solution-sub-value-sm" id="sat-vars">---</div>
          <div class="solution-sub-label" id="sat-sat-label">CLAUSES</div>
          <div class="solution-sub-value" id="sat-sat">---</div>
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
    this.cancelScan();
    (this.bannerG.node() as SVGGElement).innerHTML = "";
    (this.gridG.node() as SVGGElement).innerHTML = "";
    this.satEl.textContent = "---";
    this.satLabelEl.textContent = "CLAUSES";
    this.varsEl.textContent = "---";
  }

  protected onDispose(): void {
    this.cancelScan();
  }

  private cancelScan(): void {
    if (this.scanRafId !== null) {
      cancelAnimationFrame(this.scanRafId);
      this.scanRafId = null;
    }
    this.scanToken++;
  }

  private renderBanner(color: string, headline: string, textOpacity: number): void {
    const cx = VB_W / 2;
    (this.bannerG.node() as SVGGElement).innerHTML =
      `<rect x="0" y="0" width="${VB_W}" height="${BANNER_H}" fill="${color}"/>` +
      `<text x="${cx}" y="${(BANNER_H / 2).toFixed(2)}" text-anchor="middle" ` +
        `dominant-baseline="central" fill="rgba(255,255,255,${textOpacity})" ` +
        `font-size="34" font-weight="700" ` +
        `font-family="'JetBrains Mono', monospace" letter-spacing="2">${headline}</text>`;
  }

  protected showInstance(data: SatData) {
    this.cancelScan();

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

    // Banner starts in a neutral "SCANNING…" state. We commit to the real
    // PASS / FAIL headline once the scanline finishes sweeping the grid.
    this.renderBanner(SCAN_COLOR, "SCANNING…", 0.85);

    // Variable-assignment grid. Bottom-padded so the absolutely-positioned
    // stat boxes overlay a clean area rather than the grid itself.
    const gridH = VB_H - GRID_TOP - GRID_BOTTOM_PAD;
    const n = data.viz_count;
    let cellsHtml = "";
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
        cellsHtml += `<rect x="${(c * cellW).toFixed(3)}" y="${(r * cellH).toFixed(3)}" width="${w}" height="${h}" fill="${isTrue ? trueColor : falseColor}"/>`;
      }
    }

    // Wrap cells in a clipPath whose rect grows downward; a thin bright
    // bar rides the leading edge as the visible scanline. Both are in
    // gridG's translated coord space, so x/y stay relative to the grid.
    const scanlineColor = pass ? "rgba(220,240,220,0.85)" : "rgba(210,225,240,0.85)";
    (this.gridG.node() as SVGGElement).innerHTML = n > 0
      ? `<defs>
          <clipPath id="sat-scan-clip">
            <rect id="sat-scan-clip-rect" x="0" y="0" width="${VB_W}" height="0"/>
          </clipPath>
        </defs>
        <g clip-path="url(#sat-scan-clip)">${cellsHtml}</g>
        <rect id="sat-scanline" x="0" y="0" width="${VB_W}" height="3" fill="${scanlineColor}"/>`
      : "";

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
    this.varsEl.textContent = data.num_variables <= 10000
      ? data.num_variables.toLocaleString()
      : `showing ${data.viz_count.toLocaleString()}`;

    const finalBannerColor = pass ? PASS_COLOR : FAIL_COLOR;
    const finalHeadline = pass ? "SATISFIED" : "FAILED TO SATISFY";
    const commitBanner = () => this.renderBanner(finalBannerColor, finalHeadline, 0.96);

    // Nothing to sweep (empty grid) — commit immediately.
    if (n <= 0 || gridH <= 0) {
      commitBanner();
      return;
    }

    const gridNode = this.gridG.node() as SVGGElement;
    const clipRect = gridNode.querySelector("#sat-scan-clip-rect") as SVGRectElement | null;
    const scanLine = gridNode.querySelector("#sat-scanline") as SVGRectElement | null;
    if (!clipRect || !scanLine) {
      commitBanner();
      return;
    }

    const token = ++this.scanToken;
    const start = performance.now();
    const step = (now: number) => {
      if (token !== this.scanToken) return; // superseded by a newer render
      const t = Math.min(1, (now - start) / SCAN_DURATION_MS);
      const eased = 1 - Math.pow(1 - t, 3);
      const y = eased * gridH;
      clipRect.setAttribute("height", y.toFixed(2));
      scanLine.setAttribute("y", Math.min(y, gridH - 3).toFixed(2));
      if (t < 1) {
        this.scanRafId = requestAnimationFrame(step);
      } else {
        scanLine.setAttribute("opacity", "0");
        this.scanRafId = null;
        commitBanner();
      }
    };
    this.scanRafId = requestAnimationFrame(step);
  }
}
