import * as d3 from "d3";
import type { Panel, WSMessage } from "../types";
import { getAgentColor } from "../lib/colors";
import { formatScore } from "../lib/format";

interface EnergyData {
  num_steps: number;
  num_batteries: number;
  agg_charge: number[];
  agg_discharge: number[];
  avg_da_price: number[];
}

type AllEnergyData = Record<string, EnergyData>;

interface HistoryEntry {
  experiment_id: string;
  agent_name: string;
  agent_id?: string;
  score: number;
  route_data: AllEnergyData;
  created_at: string;
}

const MARGIN = { top: 12, right: 52, bottom: 32, left: 52 };
const VB_W = 1000;
const VB_H = 500;
const CHART_W = VB_W - MARGIN.left - MARGIN.right;
const CHART_H = VB_H - MARGIN.top - MARGIN.bottom;

export class EnergyPanel implements Panel {
  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private xAxisG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private yLeftAxisG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private yRightAxisG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;

  private scoreEl!: HTMLElement;
  private scoreDeltaEl!: HTMLElement;
  private batteriesEl!: HTMLElement;
  private instanceLabelEl!: HTMLElement;
  private navEl!: HTMLElement;
  private agentNameEl!: HTMLElement;
  private historyNavEl!: HTMLElement;
  private historyLabelEl!: HTMLElement;
  private historyLiveBtnEl!: HTMLElement;

  private allInstances: AllEnergyData = {};
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
      <div class="panel-inner energy-panel">
        <div class="panel-label">ENERGY SCHEDULE</div>
        <div class="energy-agent-name" id="energy-agent-name"></div>
        <div class="routes-history-nav" id="energy-history-nav" style="display:none">
          <button class="routes-nav-btn" id="energy-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="routes-history-label" id="energy-history-label"></span>
          <button class="routes-nav-btn" id="energy-hist-next" title="Next global best">&rsaquo;</button>
          <button class="routes-history-live" id="energy-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="routes-nav" id="energy-nav" style="display:none">
          <button class="routes-nav-btn" id="energy-prev">&lsaquo;</button>
          <span class="routes-instance-label" id="energy-instance-label"></span>
          <button class="routes-nav-btn" id="energy-next">&rsaquo;</button>
        </div>
        <div class="energy-svg-wrap" id="energy-svg-wrap">
          <svg id="energy-svg"></svg>
        </div>
        <div class="energy-batteries-box">
          <div class="routes-sub-label">BATTERIES</div>
          <div class="routes-sub-value" id="energy-batteries">---</div>
        </div>
        <div class="routes-score">
          <div class="routes-score-label">SCORE</div>
          <div class="routes-score-value" id="energy-score">---</div>
          <div class="routes-score-delta" id="energy-score-delta"></div>
        </div>
      </div>
    `;

    this.scoreEl = document.getElementById("energy-score")!;
    this.scoreDeltaEl = document.getElementById("energy-score-delta")!;
    this.batteriesEl = document.getElementById("energy-batteries")!;
    this.instanceLabelEl = document.getElementById("energy-instance-label")!;
    this.navEl = document.getElementById("energy-nav")!;
    this.agentNameEl = document.getElementById("energy-agent-name")!;
    this.historyNavEl = document.getElementById("energy-history-nav")!;
    this.historyLabelEl = document.getElementById("energy-history-label")!;
    this.historyLiveBtnEl = document.getElementById("energy-hist-live")!;

    document.getElementById("energy-prev")!.addEventListener("click", () => this.navigate(-1));
    document.getElementById("energy-next")!.addEventListener("click", () => this.navigate(1));
    document.getElementById("energy-hist-prev")!.addEventListener("click", () => this.navigateHistory(-1));
    document.getElementById("energy-hist-next")!.addEventListener("click", () => this.navigateHistory(1));
    this.historyLiveBtnEl.addEventListener("click", () => {
      if (!this.historyEntries.length) return;
      this.historyIndex = this.historyEntries.length - 1;
      this.applyHistoryEntry();
    });

    this.svg = d3.select("#energy-svg") as any;
    this.svg
      .attr("viewBox", `0 0 ${VB_W} ${VB_H}`)
      .attr("preserveAspectRatio", "xMidYMid meet");

    this.chartG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top})`) as any;
    this.xAxisG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top + CHART_H})`) as any;
    this.yLeftAxisG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top})`) as any;
    this.yRightAxisG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left + CHART_W},${MARGIN.top})`) as any;

    const wrap = document.getElementById("energy-svg-wrap")!;
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
      const res = await fetch(`${this.apiUrl}/api/replay`);
      if (!res.ok) return;
      const rows: any[] = await res.json();
      const fetched: HistoryEntry[] = rows
        .filter((r) => r && r.route_data)
        .map((r) => ({
          experiment_id: r.experiment_id,
          agent_name: r.agent_name,
          agent_id: r.agent_id,
          score: r.score,
          route_data: r.route_data as AllEnergyData,
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
    this.allInstances = entry.route_data;

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
      this.chartG.selectAll("*").remove();
      this.xAxisG.selectAll("*").remove();
      this.yLeftAxisG.selectAll("*").remove();
      this.yRightAxisG.selectAll("*").remove();
      this.scoreEl.textContent = "---";
      this.scoreDeltaEl.textContent = "";
      this.batteriesEl.textContent = "---";
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

    if (msg.type === "new_global_best" && msg.route_data) {
      const entry: HistoryEntry = {
        experiment_id: msg.experiment_id,
        agent_name: msg.agent_name,
        agent_id: msg.agent_id,
        score: msg.score,
        route_data: msg.route_data as unknown as AllEnergyData,
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

  private showInstance(data: EnergyData) {
    this.chartG.selectAll("*").remove();
    this.xAxisG.selectAll("*").remove();
    this.yLeftAxisG.selectAll("*").remove();
    this.yRightAxisG.selectAll("*").remove();

    if (!data || !data.agg_charge || !data.agg_charge.length) {
      this.batteriesEl.textContent = "---";
      return;
    }

    const n = data.num_steps;
    const dt = 0.25;

    const x = d3.scaleLinear().domain([0, n * dt]).range([0, CHART_W]);

    const powerMax = Math.max(
      d3.max(data.agg_discharge) || 0,
      Math.abs(d3.min(data.agg_charge) || 0),
      1,
    );
    const yPower = d3.scaleLinear()
      .domain([-powerMax * 1.1, powerMax * 1.1])
      .range([CHART_H, 0]);

    const priceExtent = d3.extent(data.avg_da_price) as [number, number];
    const priceMin = (priceExtent[0] ?? 0) * 0.9;
    const priceMax = (priceExtent[1] ?? 100) * 1.1;
    const yPrice = d3.scaleLinear()
      .domain([priceMin, priceMax])
      .range([CHART_H, 0]);

    // zero line
    this.chartG.append("line")
      .attr("x1", 0).attr("x2", CHART_W)
      .attr("y1", yPower(0)).attr("y2", yPower(0))
      .attr("stroke", "rgba(255,255,255,0.15)")
      .attr("stroke-width", 0.5);

    // bars
    const barW = Math.max(0.5, CHART_W / n - 0.5);
    for (let t = 0; t < n; t++) {
      const xPos = x(t * dt);
      const charge = data.agg_charge[t];
      const discharge = data.agg_discharge[t];

      if (discharge > 0) {
        this.chartG.append("rect")
          .attr("x", xPos)
          .attr("y", yPower(discharge))
          .attr("width", barW)
          .attr("height", yPower(0) - yPower(discharge))
          .attr("fill", "#ef5350")
          .attr("opacity", 0.8);
      }
      if (charge < 0) {
        this.chartG.append("rect")
          .attr("x", xPos)
          .attr("y", yPower(0))
          .attr("width", barW)
          .attr("height", yPower(charge) - yPower(0))
          .attr("fill", "#42a5f5")
          .attr("opacity", 0.8);
      }
    }

    // DA price line
    if (data.avg_da_price.length > 0) {
      const priceLine = d3.line<number>()
        .x((_, i) => x(i * dt))
        .y((d) => yPrice(d));

      this.chartG.append("path")
        .datum(data.avg_da_price)
        .attr("d", priceLine as any)
        .attr("fill", "none")
        .attr("stroke", "#ffd740")
        .attr("stroke-width", 1.5)
        .attr("opacity", 0.9);
    }

    // axes
    const xTicks = d3.axisBottom(x).ticks(8).tickFormat((d) => `${d}h`);
    this.xAxisG.call(xTicks as any)
      .selectAll("text").attr("fill", "#3d4a5c").attr("font-size", 9);
    this.xAxisG.selectAll("line").attr("stroke", "#3d4a5c");
    this.xAxisG.select(".domain").attr("stroke", "#3d4a5c");

    const yLeftTicks = d3.axisLeft(yPower).ticks(6).tickFormat((d) => `${d}`);
    this.yLeftAxisG.call(yLeftTicks as any)
      .selectAll("text").attr("fill", "#3d4a5c").attr("font-size", 9);
    this.yLeftAxisG.selectAll("line").attr("stroke", "#3d4a5c");
    this.yLeftAxisG.select(".domain").attr("stroke", "#3d4a5c");

    // left axis label
    this.yLeftAxisG.append("text")
      .attr("transform", "rotate(-90)")
      .attr("x", -CHART_H / 2).attr("y", -38)
      .attr("text-anchor", "middle")
      .attr("fill", "#5a6a7e")
      .attr("font-size", 9)
      .text("MW");

    const yRightTicks = d3.axisRight(yPrice).ticks(6).tickFormat((d) => `$${d}`);
    this.yRightAxisG.call(yRightTicks as any)
      .selectAll("text").attr("fill", "#ffd740").attr("font-size", 9);
    this.yRightAxisG.selectAll("line").attr("stroke", "rgba(255,215,64,0.3)");
    this.yRightAxisG.select(".domain").attr("stroke", "rgba(255,215,64,0.3)");

    // right axis label
    this.yRightAxisG.append("text")
      .attr("transform", "rotate(90)")
      .attr("x", CHART_H / 2).attr("y", -40)
      .attr("text-anchor", "middle")
      .attr("fill", "#ffd740")
      .attr("font-size", 9)
      .text("$/MWh");

    // legend
    const legendY = -2;
    this.chartG.append("rect")
      .attr("x", 4).attr("y", legendY).attr("width", 10).attr("height", 10)
      .attr("fill", "#ef5350").attr("opacity", 0.8);
    this.chartG.append("text")
      .attr("x", 18).attr("y", legendY + 9)
      .attr("fill", "#8a9bb5").attr("font-size", 9).text("Discharge");

    this.chartG.append("rect")
      .attr("x", 84).attr("y", legendY).attr("width", 10).attr("height", 10)
      .attr("fill", "#42a5f5").attr("opacity", 0.8);
    this.chartG.append("text")
      .attr("x", 98).attr("y", legendY + 9)
      .attr("fill", "#8a9bb5").attr("font-size", 9).text("Charge");

    this.chartG.append("line")
      .attr("x1", 152).attr("x2", 162).attr("y1", legendY + 5).attr("y2", legendY + 5)
      .attr("stroke", "#ffd740").attr("stroke-width", 1.5);
    this.chartG.append("text")
      .attr("x", 166).attr("y", legendY + 9)
      .attr("fill", "#8a9bb5").attr("font-size", 9).text("DA Price");

    this.batteriesEl.textContent = String(data.num_batteries);
  }
}
