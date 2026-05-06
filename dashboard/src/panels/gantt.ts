import * as d3 from "d3";
import type { Panel, WSMessage } from "../types";
import { getAgentColor } from "../lib/colors";
import { formatScore } from "../lib/format";
import { liveSwitchToActive, shouldShowLiveButton } from "../lib/panelLive";

interface GanttBar {
  job: number;
  op: number;
  machine: number;
  start: number;
  end: number;
}

interface GanttData {
  num_machines: number;
  num_jobs: number;
  makespan: number;
  bars: GanttBar[];
}

type AllGanttData = Record<string, GanttData>;

interface HistoryEntry {
  experiment_id: string;
  agent_name: string;
  agent_id?: string;
  score: number;
  solution_data: AllGanttData;
  created_at: string;
}

const MARGIN = { top: 8, right: 10, bottom: 28, left: 48 };
const VB_W = 1000;
const VB_H = 600;
const CHART_W = VB_W - MARGIN.left - MARGIN.right;
const CHART_H = VB_H - MARGIN.top - MARGIN.bottom;

function jobColor(job: number): string {
  const hue = (job * 137.508) % 360;
  const sat = 60 + (job % 3) * 10;
  const lit = 52 + (job % 2) * 8;
  return `hsl(${hue}, ${sat}%, ${lit}%)`;
}

export class GanttPanel implements Panel {
  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private axisG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private labelG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;

  private scoreEl!: HTMLElement;
  private scoreDeltaEl!: HTMLElement;
  private makespanEl!: HTMLElement;
  private instanceLabelEl!: HTMLElement;
  private navEl!: HTMLElement;
  private agentNameEl!: HTMLElement;
  private historyNavEl!: HTMLElement;
  private historyLabelEl!: HTMLElement;
  private historyLiveBtnEl!: HTMLElement;
  private emptyStateEl!: HTMLElement;
  private historyLoaded = false;

  private allInstances: AllGanttData = {};
  private currentIndex = 0;
  private rawScore: number | null = null;
  private numInstances = 1;

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
      <div class="panel-inner gantt-panel">
        <div class="panel-label">SCHEDULE</div>
        <div class="gantt-agent-name" id="gantt-agent-name"></div>
        <div class="solution-history-nav" id="gantt-history-nav" style="display:none">
          <button class="solution-nav-btn" id="gantt-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="solution-history-label" id="gantt-history-label"></span>
          <button class="solution-nav-btn" id="gantt-hist-next" title="Next global best">&rsaquo;</button>
          <button class="solution-history-live" id="gantt-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="solution-nav" id="gantt-nav" style="display:none">
          <button class="solution-nav-btn" id="gantt-prev">&lsaquo;</button>
          <span class="solution-instance-label" id="gantt-instance-label"></span>
          <button class="solution-nav-btn" id="gantt-next">&rsaquo;</button>
        </div>
        <div class="gantt-svg-wrap" id="gantt-svg-wrap">
          <svg id="gantt-svg"></svg>
          <div class="solution-empty-state" id="gantt-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="gantt-makespan-box">
          <div class="solution-sub-label">MAKESPAN</div>
          <div class="solution-sub-value" id="gantt-makespan">---</div>
        </div>
        <div class="solution-score">
          <div class="solution-score-label">SCORE</div>
          <div class="solution-score-value" id="gantt-score">---</div>
          <div class="solution-score-delta" id="gantt-score-delta"></div>
        </div>
      </div>
    `;

    this.scoreEl = document.getElementById("gantt-score")!;
    this.scoreDeltaEl = document.getElementById("gantt-score-delta")!;
    this.makespanEl = document.getElementById("gantt-makespan")!;
    this.instanceLabelEl = document.getElementById("gantt-instance-label")!;
    this.navEl = document.getElementById("gantt-nav")!;
    this.agentNameEl = document.getElementById("gantt-agent-name")!;
    this.historyNavEl = document.getElementById("gantt-history-nav")!;
    this.historyLabelEl = document.getElementById("gantt-history-label")!;
    this.historyLiveBtnEl = document.getElementById("gantt-hist-live")!;
    this.emptyStateEl = document.getElementById("gantt-empty-state")!;

    document.getElementById("gantt-prev")!.addEventListener("click", () => this.navigate(-1));
    document.getElementById("gantt-next")!.addEventListener("click", () => this.navigate(1));
    document.getElementById("gantt-hist-prev")!.addEventListener("click", () => this.navigateHistory(-1));
    document.getElementById("gantt-hist-next")!.addEventListener("click", () => this.navigateHistory(1));
    this.historyLiveBtnEl.addEventListener("click", () => {
      // Non-active challenge → switch viewed to active. Active
      // challenge → fall through to "jump to latest history".
      if (liveSwitchToActive("job_scheduling")) return;
      if (!this.historyEntries.length) return;
      this.historyIndex = this.historyEntries.length - 1;
      this.applyHistoryEntry();
    });

    this.svg = d3.select("#gantt-svg") as any;
    this.svg
      .attr("viewBox", `0 0 ${VB_W} ${VB_H}`)
      .attr("preserveAspectRatio", "xMidYMid meet");

    this.chartG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top})`) as any;
    this.labelG = this.svg.append("g")
      .attr("transform", `translate(0,${MARGIN.top})`) as any;
    this.axisG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top + CHART_H})`) as any;

    const wrap = document.getElementById("gantt-svg-wrap")!;
    const resize = () => {
      this.svg.attr("width", wrap.clientWidth).attr("height", wrap.clientHeight);
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
      const res = await fetch(`${this.apiUrl}/api/replay?challenge=job_scheduling`);
      if (!res.ok) return;
      const rows: any[] = await res.json();
      const fetched: HistoryEntry[] = rows
        .filter((r) => r && r.solution_data)
        .map((r) => ({
          experiment_id: r.experiment_id,
          agent_name: r.agent_name,
          agent_id: r.agent_id,
          score: r.score,
          solution_data: r.solution_data as AllGanttData,
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
      this.historyLoaded = true;
      this.updateHistoryLabel();
      this.updateEmptyState();
    } catch {
      // non-fatal
      this.historyLoaded = true;
      this.updateEmptyState();
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
    this.updateEmptyState();
  }

  private updateHistoryLabel() {
    const total = this.historyEntries.length;
    const atLatest = this.isAtLatest();
    const showLive = shouldShowLiveButton("job_scheduling", atLatest);
    if (total <= 1 && !showLive) {
      this.historyNavEl.style.display = "none";
      return;
    }
    this.historyNavEl.style.display = "flex";
    this.historyLiveBtnEl.style.display = showLive ? "inline-block" : "none";
    const suffix = atLatest ? " · LATEST" : "";
    this.historyLabelEl.textContent =
      total > 0 ? `BEST ${this.historyIndex + 1}/${total}${suffix}` : "";
  }

  private updateEmptyState() {
    if (!this.emptyStateEl) return;
    // Hide while we're still fetching the initial replay so the
    // user doesn't see "Challenge not started yet" flash for a few
    // seconds during the in-flight load. Only show the overlay
    // once we've definitively confirmed the channel has no data.
    const showEmpty = this.historyLoaded && this.historyEntries.length === 0;
    this.emptyStateEl.style.display = showEmpty ? "flex" : "none";
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
      this.updateHistoryLabel();
      this.updateEmptyState();
      this.chartG.selectAll("*").remove();
      this.axisG.selectAll("*").remove();
      this.labelG.selectAll("*").remove();
      this.scoreEl.textContent = "---";
      this.scoreDeltaEl.textContent = "";
      this.makespanEl.textContent = "---";
      this.navEl.style.display = "none";
      this.historyNavEl.style.display = "none";
      this.instanceLabelEl.textContent = "";
      this.agentNameEl.textContent = "";
      this.agentNameEl.style.color = "";
      return;
    }

    if (msg.type === "stats_update") {
      if (msg.num_instances) this.numInstances = msg.num_instances;
      if (msg.best_score != null && this.historyEntries.length === 0) {
        this.rawScore = msg.best_score;
        this.scoreEl.textContent = formatScore(msg.best_score);
      }
    }

    if (msg.type === "new_global_best" && msg.solution_data) {
      this.historyLoaded = true;
      if (msg.num_instances) this.numInstances = msg.num_instances;

      const entry: HistoryEntry = {
        experiment_id: msg.experiment_id,
        agent_name: msg.agent_name,
        agent_id: msg.agent_id,
        score: msg.score,
        solution_data: msg.solution_data as unknown as AllGanttData,
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
          this.updateEmptyState();
        }
      }
    }
  }

  private showInstance(data: GanttData) {
    this.chartG.selectAll("*").remove();
    this.axisG.selectAll("*").remove();
    this.labelG.selectAll("*").remove();

    if (!data || !data.bars || !data.bars.length) {
      this.makespanEl.textContent = "---";
      return;
    }

    const nMachines = data.num_machines;
    const makespan = data.makespan;

    const x = d3.scaleLinear().domain([0, makespan]).range([0, CHART_W]);
    const rowH = CHART_H / nMachines;
    const barH = rowH * 0.78;
    const barPad = (rowH - barH) / 2;

    // alternating row backgrounds
    for (let m = 0; m < nMachines; m++) {
      if (m % 2 === 0) {
        this.chartG.append("rect")
          .attr("x", 0).attr("y", m * rowH)
          .attr("width", CHART_W).attr("height", rowH)
          .attr("fill", "rgba(255,255,255,0.015)");
      }
    }

    // light grid lines at time ticks
    const ticks = x.ticks(8);
    for (const t of ticks) {
      this.chartG.append("line")
        .attr("x1", x(t)).attr("x2", x(t))
        .attr("y1", 0).attr("y2", CHART_H)
        .attr("stroke", "rgba(255,255,255,0.04)")
        .attr("stroke-width", 0.5);
    }

    // bars
    for (const bar of data.bars) {
      const bx = x(bar.start);
      const bw = x(bar.end) - x(bar.start);
      const by = bar.machine * rowH + barPad;
      this.chartG.append("rect")
        .attr("x", bx).attr("y", by)
        .attr("width", Math.max(bw, 0.8))
        .attr("height", barH)
        .attr("fill", jobColor(bar.job))
        .attr("stroke", "rgba(0,0,0,0.4)")
        .attr("stroke-width", 0.4)
        .attr("rx", 1);
    }

    // makespan line
    this.chartG.append("line")
      .attr("x1", x(makespan)).attr("x2", x(makespan))
      .attr("y1", 0).attr("y2", CHART_H)
      .attr("stroke", "#ff5252")
      .attr("stroke-width", 1)
      .attr("stroke-dasharray", "4,3")
      .attr("opacity", 0.6);

    // machine labels
    const fontSize = Math.min(11, rowH * 0.55);
    for (let m = 0; m < nMachines; m++) {
      this.labelG.append("text")
        .attr("x", MARGIN.left - 5)
        .attr("y", m * rowH + rowH / 2)
        .attr("text-anchor", "end")
        .attr("dominant-baseline", "central")
        .attr("fill", "#3d4a5c")
        .attr("font-size", fontSize)
        .attr("font-family", "'JetBrains Mono', monospace")
        .text(m);
    }

    // time axis ticks
    for (const t of ticks) {
      this.axisG.append("line")
        .attr("x1", x(t)).attr("x2", x(t))
        .attr("y1", 0).attr("y2", 5)
        .attr("stroke", "#3d4a5c")
        .attr("stroke-width", 0.5);
      this.axisG.append("text")
        .attr("x", x(t)).attr("y", 16)
        .attr("text-anchor", "middle")
        .attr("fill", "#3d4a5c")
        .attr("font-size", 9)
        .attr("font-family", "'JetBrains Mono', monospace")
        .text(t);
    }

    this.makespanEl.textContent = makespan.toLocaleString();
  }
}
