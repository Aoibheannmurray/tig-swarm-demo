import * as d3 from "d3";
import type { Panel, WSMessage } from "../types";
import { getAgentColor } from "../lib/colors";
import { formatScore } from "../lib/format";

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

interface HistoryEntry {
  experiment_id: string;
  agent_name: string;
  agent_id?: string;
  score: number;
  solution_data: AllSatData;
  created_at: string;
}

const VB_W = 1000;
const VB_H = 1000;
const HIST_H = 180;             // top strip for clause-satisfaction histogram
const HIST_GAP = 20;             // gap between histogram and grid
const GRID_TOP = HIST_H + HIST_GAP;

// Stacked-bar colors per "satisfying-literal count" bucket.
const BIN_COLORS = ["#d04d4d", "#d8a13a", "#7ec043", "#3a8a3a"]; // 0,1,2,3 sats

export class SatPanel implements Panel {
  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private histG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private gridG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;

  private scoreEl!: HTMLElement;
  private scoreDeltaEl!: HTMLElement;
  private satEl!: HTMLElement;
  private varsEl!: HTMLElement;
  private instanceLabelEl!: HTMLElement;
  private navEl!: HTMLElement;
  private agentNameEl!: HTMLElement;
  private historyNavEl!: HTMLElement;
  private historyLabelEl!: HTMLElement;
  private historyLiveBtnEl!: HTMLElement;

  private allInstances: AllSatData = {};
  private currentIndex = 0;
  private rawScore: number | null = null;

  private historyEntries: HistoryEntry[] = [];
  private historyIndex = -1;
  private apiUrl = "";

  private get instanceKeys(): string[] {
    return Object.keys(this.allInstances).sort();
  }

  private isAtLatest(): boolean {
    return (
      this.historyEntries.length === 0 ||
      this.historyIndex >= this.historyEntries.length - 1
    );
  }

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="panel-inner knapsack-panel">
        <div class="panel-label">CLAUSES &amp; ASSIGNMENT</div>
        <div class="knapsack-agent-name" id="sat-agent-name"></div>
        <div class="routes-history-nav" id="sat-history-nav" style="display:none">
          <button class="routes-nav-btn" id="sat-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="routes-history-label" id="sat-history-label"></span>
          <button class="routes-nav-btn" id="sat-hist-next" title="Next global best">&rsaquo;</button>
          <button class="routes-history-live" id="sat-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="routes-nav" id="sat-nav" style="display:none">
          <button class="routes-nav-btn" id="sat-prev">&lsaquo;</button>
          <span class="routes-instance-label" id="sat-instance-label"></span>
          <button class="routes-nav-btn" id="sat-next">&rsaquo;</button>
        </div>
        <div class="knapsack-svg-wrap" id="sat-svg-wrap">
          <svg id="sat-svg"></svg>
        </div>
        <div class="knapsack-value-box">
          <div class="routes-sub-label">SATISFIED</div>
          <div class="routes-sub-value" id="sat-sat">---</div>
        </div>
        <div class="knapsack-items-box">
          <div class="routes-sub-label">VARIABLES</div>
          <div class="routes-sub-value" id="sat-vars">---</div>
        </div>
        <div class="routes-score">
          <div class="routes-score-label">SCORE</div>
          <div class="routes-score-value" id="sat-score">---</div>
          <div class="routes-score-delta" id="sat-score-delta"></div>
        </div>
      </div>
    `;

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

    document.getElementById("sat-prev")!.addEventListener("click", () => this.navigate(-1));
    document.getElementById("sat-next")!.addEventListener("click", () => this.navigate(1));
    document.getElementById("sat-hist-prev")!.addEventListener("click", () => this.navigateHistory(-1));
    document.getElementById("sat-hist-next")!.addEventListener("click", () => this.navigateHistory(1));
    this.historyLiveBtnEl.addEventListener("click", () => {
      if (!this.historyEntries.length) return;
      this.historyIndex = this.historyEntries.length - 1;
      this.applyHistoryEntry();
    });

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

    setInterval(() => {
      if (this.instanceKeys.length > 1) this.navigate(1);
    }, 8000);

    const params = new URLSearchParams(window.location.search);
    const explicit = params.get("api");
    if (explicit) {
      this.apiUrl = explicit;
    } else {
      const ws = params.get("ws") || "";
      if (ws) {
        this.apiUrl = ws.replace("ws://", "http://").replace("wss://", "https://").replace("/ws/dashboard", "");
      } else {
        this.apiUrl = `${window.location.protocol}//${window.location.host}`;
      }
    }
    this.fetchHistory();
  }

  private async fetchHistory() {
    try {
      const res = await fetch(`${this.apiUrl}/api/replay`);
      if (!res.ok) return;
      const rows: any[] = await res.json();
      const fetched: HistoryEntry[] = rows
        .filter((r) => r && r.solution_data)
        .map((r) => ({
          experiment_id: r.experiment_id,
          agent_name: r.agent_name,
          agent_id: r.agent_id,
          score: r.score,
          solution_data: r.solution_data as AllSatData,
          created_at: r.created_at,
        }));
      const existingIds = new Set(this.historyEntries.map((e) => e.experiment_id));
      const merged = [
        ...fetched.filter((e) => !existingIds.has(e.experiment_id)),
        ...this.historyEntries,
      ];
      merged.sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
      this.historyEntries = merged;
      if (this.isAtLatest() && this.historyEntries.length) {
        this.historyIndex = this.historyEntries.length - 1;
        this.applyHistoryEntry();
      }
      this.updateHistoryLabel();
    } catch {
      // non-fatal
    }
  }

  private navigateHistory(delta: number) {
    if (!this.historyEntries.length) return;
    const next = Math.max(0, Math.min(this.historyEntries.length - 1, this.historyIndex + delta));
    if (next === this.historyIndex) return;
    this.historyIndex = next;
    this.applyHistoryEntry();
  }

  private applyHistoryEntry() {
    const entry = this.historyEntries[this.historyIndex];
    if (!entry) return;

    this.rawScore = entry.score;
    this.allInstances = entry.solution_data;

    this.agentNameEl.textContent = entry.agent_name;
    this.agentNameEl.style.color = entry.agent_id ? getAgentColor(entry.agent_id) : "";

    const keys = this.instanceKeys;
    if (this.currentIndex >= keys.length) this.currentIndex = 0;
    this.updateInstanceLabel();
    if (keys.length > 0) {
      this.showInstance(this.allInstances[keys[this.currentIndex]]);
    }

    this.scoreEl.textContent = formatScore(entry.score);

    if (this.historyIndex > 0) {
      const prev = this.historyEntries[this.historyIndex - 1];
      const pct = prev.score !== 0 ? ((entry.score - prev.score) / Math.abs(prev.score)) * 100 : 0;
      const sign = pct >= 0 ? "+" : "";
      this.scoreDeltaEl.textContent = `${sign}${pct.toFixed(5)}% vs prev best`;
      this.scoreDeltaEl.style.color = "var(--green)";
    } else {
      this.scoreDeltaEl.textContent = "first global best";
      this.scoreDeltaEl.style.color = "var(--text-dim)";
    }

    this.updateHistoryLabel();
  }

  private updateHistoryLabel() {
    const total = this.historyEntries.length;
    if (total <= 1) {
      this.historyNavEl.style.display = "none";
      return;
    }
    this.historyNavEl.style.display = "flex";
    const atLatest = this.isAtLatest();
    this.historyLiveBtnEl.style.display = atLatest ? "none" : "inline-block";
    const suffix = atLatest ? " · LATEST" : "";
    this.historyLabelEl.textContent = `BEST ${this.historyIndex + 1}/${total}${suffix}`;
  }

  private navigate(delta: number) {
    const keys = this.instanceKeys;
    if (keys.length === 0) return;
    this.currentIndex = (this.currentIndex + delta + keys.length) % keys.length;
    this.updateInstanceLabel();
    this.showInstance(this.allInstances[keys[this.currentIndex]]);
  }

  private updateInstanceLabel() {
    const keys = this.instanceKeys;
    if (keys.length <= 1) {
      this.navEl.style.display = "none";
      return;
    }
    this.navEl.style.display = "flex";
    const key = keys[this.currentIndex].replace(/\.txt$/, "");
    this.instanceLabelEl.textContent = `${key}  (${this.currentIndex + 1}/${keys.length})`;
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.allInstances = {};
      this.currentIndex = 0;
      this.rawScore = null;
      this.historyEntries = [];
      this.historyIndex = -1;
      this.histG.selectAll("*").remove();
      this.gridG.selectAll("*").remove();
      this.scoreEl.textContent = "---";
      this.scoreDeltaEl.textContent = "";
      this.satEl.textContent = "---";
      this.varsEl.textContent = "---";
      this.navEl.style.display = "none";
      this.historyNavEl.style.display = "none";
      this.instanceLabelEl.textContent = "";
      this.agentNameEl.textContent = "";
      this.agentNameEl.style.color = "";
      return;
    }

    if (msg.type === "stats_update") {
      if (msg.best_score != null && this.historyEntries.length === 0) {
        this.rawScore = msg.best_score;
        this.scoreEl.textContent = formatScore(msg.best_score);
      }
    }

    if (msg.type === "new_global_best" && msg.solution_data) {
      const entry: HistoryEntry = {
        experiment_id: msg.experiment_id,
        agent_name: msg.agent_name,
        agent_id: msg.agent_id,
        score: msg.score,
        solution_data: msg.solution_data as unknown as AllSatData,
        created_at: msg.timestamp,
      };
      const existingIdx = this.historyEntries.findIndex(
        (e) => e.experiment_id === entry.experiment_id,
      );
      if (existingIdx >= 0) {
        this.historyEntries[existingIdx] = entry;
        this.historyIndex = existingIdx;
        this.applyHistoryEntry();
      } else {
        const wasAtLatest = this.isAtLatest();
        this.historyEntries.push(entry);
        if (wasAtLatest) {
          this.historyIndex = this.historyEntries.length - 1;
          this.applyHistoryEntry();
        } else {
          this.updateHistoryLabel();
        }
      }
    }
  }

  private showInstance(data: SatData) {
    this.histG.selectAll("*").remove();
    this.gridG.selectAll("*").remove();

    if (!data || !data.assignment_bits || !data.clause_bins) {
      this.satEl.textContent = "---";
      this.varsEl.textContent = "---";
      return;
    }

    // ── Top strip: stacked clause-satisfaction histogram ──────────────
    const bins = data.clause_bins;
    const numBins = bins.length;
    const binW = VB_W / numBins;
    const maxBinTotal = bins.reduce(
      (m, b) => Math.max(m, b[0] + b[1] + b[2] + b[3]),
      1,
    );
    for (let bi = 0; bi < numBins; bi++) {
      let yCursor = HIST_H;
      const bin = bins[bi];
      for (let k = 0; k < 4; k++) {
        const segH = (bin[k] / maxBinTotal) * HIST_H;
        if (segH <= 0) continue;
        yCursor -= segH;
        this.histG.append("rect")
          .attr("x", bi * binW)
          .attr("y", yCursor)
          .attr("width", binW - 0.6)
          .attr("height", segH)
          .attr("fill", BIN_COLORS[k]);
      }
    }
    // Light divider line under the histogram.
    this.histG.append("line")
      .attr("x1", 0).attr("x2", VB_W)
      .attr("y1", HIST_H + 1).attr("y2", HIST_H + 1)
      .attr("stroke", "rgba(255,255,255,0.18)")
      .attr("stroke-width", 1);

    // ── Bottom: variable-assignment grid ─────────────────────────────
    const gridH = VB_H - GRID_TOP;
    const n = data.viz_count;
    if (n > 0) {
      // Choose grid dimensions so cells fill the area roughly square.
      const aspect = VB_W / gridH;
      const cols = Math.max(1, Math.round(Math.sqrt(n * aspect)));
      const rows = Math.ceil(n / cols);
      const cellW = VB_W / cols;
      const cellH = gridH / rows;
      const trueColor = "#4a7fd6";       // T = solid blue
      const falseColor = "rgba(255,255,255,0.06)"; // F = faint background
      const bits = data.assignment_bits;

      for (let i = 0; i < n; i++) {
        const r = Math.floor(i / cols);
        const c = i % cols;
        const isTrue = bits.charCodeAt(i) === 49; // "1"
        this.gridG.append("rect")
          .attr("x", c * cellW)
          .attr("y", r * cellH)
          .attr("width", Math.max(0.5, cellW - 0.4))
          .attr("height", Math.max(0.5, cellH - 0.4))
          .attr("fill", isTrue ? trueColor : falseColor);
      }
    }

    // ── Stats overlays ───────────────────────────────────────────────
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
