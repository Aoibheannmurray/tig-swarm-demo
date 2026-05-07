import * as d3 from "d3";
import type { Panel, WSMessage } from "../types";
import { getAgentColor } from "../lib/colors";
import { formatScore } from "../lib/format";
import { liveSwitchToActive, shouldShowLiveButton } from "../lib/panelLive";

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

interface HistoryEntry {
  experiment_id: string;
  agent_name: string;
  agent_id?: string;
  score: number;
  solution_data: AllKnapsackData;
  created_at: string;
}

const MARGIN = { top: 8, right: 8, bottom: 8, left: 8 };
const VB_W = 1000;
const VB_H = 1000;
const CHART_W = VB_W - MARGIN.left - MARGIN.right;
const CHART_H = VB_H - MARGIN.top - MARGIN.bottom;

export class KnapsackPanel implements Panel {
  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;

  private scoreEl!: HTMLElement;
  private scoreDeltaEl!: HTMLElement;
  private valueEl!: HTMLElement;
  private itemsEl!: HTMLElement;
  private instanceLabelEl!: HTMLElement;
  private navEl!: HTMLElement;
  private agentNameEl!: HTMLElement;
  private historyNavEl!: HTMLElement;
  private historyLabelEl!: HTMLElement;
  private historyLiveBtnEl!: HTMLElement;
  private emptyStateEl!: HTMLElement;
  private historyLoaded = false;

  private allInstances: AllKnapsackData = {};
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

    document.getElementById("knapsack-prev")!.addEventListener("click", () => this.navigate(-1));
    document.getElementById("knapsack-next")!.addEventListener("click", () => this.navigate(1));
    document.getElementById("knapsack-hist-prev")!.addEventListener("click", () => this.navigateHistory(-1));
    document.getElementById("knapsack-hist-next")!.addEventListener("click", () => this.navigateHistory(1));
    this.historyLiveBtnEl.addEventListener("click", () => {
      // Non-active challenge → switch viewed to active. Active
      // challenge → fall through to "jump to latest history".
      if (liveSwitchToActive("knapsack")) return;
      if (!this.historyEntries.length) return;
      this.historyIndex = this.historyEntries.length - 1;
      this.applyHistoryEntry();
    });

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
      const res = await fetch(`${this.apiUrl}/api/replay?challenge=knapsack`);
      if (!res.ok) return;
      const rows: any[] = await res.json();
      const fetched: HistoryEntry[] = rows
        .filter((r) => r && r.solution_data)
        .map((r) => ({
          experiment_id: r.experiment_id,
          agent_name: r.agent_name,
          agent_id: r.agent_id,
          score: r.score,
          solution_data: r.solution_data as AllKnapsackData,
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
    const showLive = shouldShowLiveButton("knapsack", atLatest);
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
      this.scoreEl.textContent = "---";
      this.scoreDeltaEl.textContent = "";
      this.valueEl.textContent = "---";
      this.itemsEl.textContent = "---";
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
      this.historyLoaded = true;
      const entry: HistoryEntry = {
        experiment_id: msg.experiment_id,
        agent_name: msg.agent_name,
        agent_id: msg.agent_id,
        score: msg.score,
        solution_data: msg.solution_data as unknown as AllKnapsackData,
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

  private showInstance(data: KnapsackData) {
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

    const colorScale = d3.scaleSequential(d3.interpolateRdYlBu)
      .domain([maxVal, minVal]);

    // Build grid as a single SVG string. With k up to ~32 we can
    // generate >1000 rects per redraw — per-element d3.append calls
    // become a measurable lag.
    const w = cellSize.toFixed(3);
    const cells: string[] = [];
    for (let i = 0; i < k; i++) {
      const yPos = (i * cellSize).toFixed(3);
      const rowVals = data.interaction_values[i];
      for (let j = 0; j < k; j++) {
        const xPos = (j * cellSize).toFixed(3);
        const v = rowVals[j];
        const fill = i === j
          ? "rgba(255,255,255,0.03)"
          : v === 0
            ? "rgba(255,255,255,0.02)"
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
