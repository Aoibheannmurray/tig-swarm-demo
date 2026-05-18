import { max, min } from "d3-array";
import { scaleLinear } from "d3-scale";
import { select } from "d3-selection";
import { symbol, symbolDiamond, symbolSquare, symbolStar } from "d3-shape";
import { getAgentColor, token } from "../lib/colors";
import { formatScore } from "../lib/format";
import { isBetter } from "../lib/swarmConfig";
import { getViewedChallenge } from "../lib/viewedChallenge";
import type { Panel, WSMessage } from "../types";

const AXIS_TEXT = () => token("--ink-dim", "rgba(26,26,26,0.50)");
const GRID_LINE = () => token("--border-subtle", "rgba(26,26,26,0.08)");

// Axis font scales with chart width so /benchmark.html (full-screen, ~1600px+)
// gets readable axis labels while the multi-panel home grid (~600px) stays
// compact. Clamps to [10, 22] px so it never goes microscopic on a phone or
// gigantic on an ultrawide.
const axisFontPx = (width: number) =>
  Math.min(22, Math.max(10, Math.round(width / 90)));

interface DataPoint {
  time: number; // ms since start
  score: number;
  agentName?: string;
  agentId?: string;
  isBreakthrough?: boolean;
}

type Tab =
  | { type: "global" }
  | { type: "agent"; agentId: string; agentName: string };

interface AgentExperiment {
  time: number;
  score: number;
  feasible: boolean;
  experimentId?: string;
  // Per-iteration metadata fed back from /api/agent_experiments — used to
  // mark events on the per-agent progress plot:
  //  - trajectoryId   → group of consecutive experiments sharing a trajectory
  //  - trajectoryDeactivated → last experiment on a trajectory that became
  //                            inactive (cross marker)
  //  - receivedHint   → "tacit_knowledge" (star) / "inspiration" (square)
  trajectoryId?: string | null;
  trajectoryDeactivated?: boolean;
  receivedHint?: "tacit_knowledge" | "inspiration" | null;
}

interface AgentProgress {
  registeredAt: number; // epoch ms
  experiments: AgentExperiment[]; // time = ms since registeredAt
  experimentIds: Set<string>;
  loaded: boolean;
  lastEventTime: number; // epoch ms of most recent appended experiment
}

export class ChartPanel implements Panel {
  private svg!: any;
  private g!: any;
  private globalData: DataPoint[] = [];
  private globalStartTime = 0;
  private width = 0;
  private height = 0;

  private apiUrl = "";

  private tabs: Tab[] = [{ type: "global" }];
  private currentTabIndex = 0;

  private agentProgress = new Map<string, AgentProgress>();
  // Live experiment events that arrive before /api/agent_experiments has
  // finished loading for a given agent.
  private pendingAgentExperiments = new Map<string, any[]>();

  private tabLabelEl!: HTMLElement;
  private tabPrevEl!: HTMLElement;
  private tabNextEl!: HTMLElement;
  private redrawScheduled = false;

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="panel-inner chart-panel">
        <div class="panel-label">BENCHMARK PROGRESS</div>
        <div class="chart-tabs" id="chart-tabs">
          <button class="chart-tab-btn" id="chart-tab-prev" type="button">&lsaquo;</button>
          <span class="chart-tab-label" id="chart-tab-label">GLOBAL</span>
          <button class="chart-tab-btn" id="chart-tab-next" type="button">&rsaquo;</button>
        </div>
        <svg id="chart-svg"></svg>
      </div>
    `;

    this.tabLabelEl = document.getElementById("chart-tab-label")!;
    this.tabPrevEl = document.getElementById("chart-tab-prev")!;
    this.tabNextEl = document.getElementById("chart-tab-next")!;

    this.tabPrevEl.addEventListener("click", () => this.cycleTab(-1));
    this.tabNextEl.addEventListener("click", () => this.cycleTab(1));

    const svgEl = document.getElementById("chart-svg")!;
    const rect = svgEl.parentElement!.getBoundingClientRect();
    this.width = rect.width;
    this.height = rect.height - 48; // label + tab row

    this.svg = select("#chart-svg")
      .attr("width", this.width)
      .attr("height", this.height);

    this.g = this.svg.append("g");

    // Resolve API base URL the same way other panels do.
    const params = new URLSearchParams(window.location.search);
    const explicit = params.get("api");
    if (explicit) this.apiUrl = explicit;
    else {
      const ws = params.get("ws") || "";
      if (ws) {
        this.apiUrl = ws
          .replace("ws://", "http://")
          .replace("wss://", "https://")
          .replace("/ws/dashboard", "");
      } else {
        this.apiUrl = `${window.location.protocol}//${window.location.host}`;
      }
    }

    const observer = new ResizeObserver(() => {
      const newRect = svgEl.parentElement!.getBoundingClientRect();
      this.width = newRect.width;
      this.height = newRect.height - 48;
      this.svg.attr("width", this.width).attr("height", this.height);
      this.redraw();
    });
    observer.observe(svgEl.parentElement!);

    this.renderTabLabel();
  }

  // Seed the chart with the full best-so-far trajectory in one batch.
  // `entries` must be in chronological order. Called on initial load so the
  // chart reflects the entire run, not just the recent-20 window returned by
  // /api/state.
  //
  // We apply a running-minimum filter: server-side best_history can contain
  // non-improving rows (seen in practice after resets and from a race in the
  // is_new_best check), but the chart is a best-so-far trajectory, so only
  // strictly-improving points belong on it.
  seedHistory(entries: { score: number; agent_name: string; agent_id?: string; created_at: string }[]) {
    if (!entries.length) {
      // Empty replay — clear any prior data and let redraw show the
      // "No iterations yet" placeholder for the viewed challenge.
      this.globalData = [];
      this.globalStartTime = 0;
      if (this.currentTab().type === "global") this.redraw();
      return;
    }
    const first = new Date(entries[0].created_at).getTime();
    this.globalStartTime = first;
    const filtered: DataPoint[] = [];
    let runningBest: number | null = null;
    for (const e of entries) {
      if (runningBest !== null && !isBetter(e.score, runningBest)) continue;
      runningBest = e.score;
      filtered.push({
        time: Math.max(0, new Date(e.created_at).getTime() - first),
        score: e.score,
        agentName: e.agent_name,
        agentId: e.agent_id,
        isBreakthrough: true,
      });
    }
    this.globalData = filtered;
    if (this.currentTab().type === "global") this.redraw();
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.globalData = [];
      this.globalStartTime = 0;
      this.agentProgress.clear();
      this.pendingAgentExperiments.clear();
      this.tabs = [{ type: "global" }];
      this.currentTabIndex = 0;
      this.renderTabLabel();
      this.g.selectAll("*").remove();
      return;
    }

    if (msg.type === "leaderboard_update") {
      this.syncTabsFromLeaderboard(msg.entries);
    }

    if (msg.type === "experiment_published") {
      this.updateGlobalFromMessage(msg);
      this.appendAgentExperiment(msg);
    }
  }

  // ── Tab navigation ──

  private currentTab(): Tab {
    return this.tabs[this.currentTabIndex];
  }

  private cycleTab(delta: number) {
    if (this.tabs.length === 0) return;
    this.currentTabIndex = (this.currentTabIndex + delta + this.tabs.length) % this.tabs.length;
    this.renderTabLabel();
    const tab = this.currentTab();
    if (tab.type === "agent") {
      this.ensureAgentLoaded(tab.agentId).then(() => {
        if (this.currentTab().type === "agent"
            && (this.currentTab() as any).agentId === tab.agentId) {
          this.redraw();
        }
      });
    } else {
      this.redraw();
    }
  }

  private renderTabLabel() {
    const tab = this.currentTab();
    if (tab.type === "global") {
      this.tabLabelEl.textContent = "GLOBAL";
      this.tabLabelEl.style.color = "";
    } else {
      this.tabLabelEl.textContent = tab.agentName;
      this.tabLabelEl.style.color = getAgentColor(tab.agentId);
    }
  }

  private syncTabsFromLeaderboard(entries: { agent_id: string; agent_name: string }[]) {
    const currentTab = this.currentTab();
    const activeAgentId = currentTab.type === "agent" ? currentTab.agentId : null;

    // Keep GLOBAL first, then agents in leaderboard order.
    const newTabs: Tab[] = [{ type: "global" }];
    for (const entry of entries) {
      if (!entry.agent_id) continue;
      newTabs.push({
        type: "agent",
        agentId: entry.agent_id,
        agentName: entry.agent_name,
      });
    }
    this.tabs = newTabs;

    // Preserve the user's current selection across reorderings.
    if (activeAgentId) {
      const idx = this.tabs.findIndex(
        (t) => t.type === "agent" && t.agentId === activeAgentId
      );
      this.currentTabIndex = idx >= 0 ? idx : 0;
    } else {
      this.currentTabIndex = Math.min(this.currentTabIndex, this.tabs.length - 1);
    }
    this.renderTabLabel();
  }

  // ── Global chart data (existing behavior) ──

  private updateGlobalFromMessage(msg: any) {
    if (!msg.feasible) return;
    const msgTime = msg.timestamp ? new Date(msg.timestamp).getTime() : Date.now();
    if (this.globalStartTime === 0) this.globalStartTime = msgTime;
    const time = msgTime - this.globalStartTime;

    const tryAppend = () => {
      this.globalData.push({
        time: Math.max(0, time),
        score: msg.score,
        agentName: msg.agent_name,
        agentId: msg.agent_id,
        isBreakthrough: msg.is_new_best,
      });
      if (this.currentTab().type === "global") this.redraw();
    };

    if (this.globalData.length === 0) {
      tryAppend();
    } else {
      const currentBest = this.globalData[this.globalData.length - 1].score;
      if (isBetter(msg.score, currentBest)) tryAppend();
    }
  }

  // ── Per-agent chart data ──

  private async ensureAgentLoaded(agentId: string): Promise<void> {
    const existing = this.agentProgress.get(agentId);
    if (existing?.loaded) return;

    try {
      // Pin to the viewed challenge — without this, the server falls back
      // to its active challenge (resolve_challenge in server.py), so an
      // agent viewed on a non-active challenge returns zero experiments
      // and the per-agent tab shows "no attempts yet from <name>".
      const challenge = getViewedChallenge();
      const res = await fetch(
        `${this.apiUrl}/api/agent_experiments` +
          `?agent_id=${encodeURIComponent(agentId)}` +
          `&challenge=${encodeURIComponent(challenge)}`,
      );
      if (!res.ok) return;
      const data: {
        agent_id: string;
        agent_name: string | null;
        registered_at: string | null;
        experiments: {
          id?: string;
          score: number;
          feasible: boolean;
          created_at: string;
          trajectory_id?: string | null;
          received_hint?: "tacit_knowledge" | "inspiration" | null;
          trajectory_deactivated?: boolean;
        }[];
      } = await res.json();

      const registeredAt = data.registered_at
        ? new Date(data.registered_at).getTime()
        : Date.now();

      const experiments: AgentExperiment[] = data.experiments.map((e) => ({
        time: Math.max(0, new Date(e.created_at).getTime() - registeredAt),
        score: e.score,
        feasible: e.feasible,
        experimentId: e.id,
        trajectoryId: e.trajectory_id ?? null,
        trajectoryDeactivated: !!e.trajectory_deactivated,
        receivedHint: e.received_hint ?? null,
      }));

      const experimentIds = new Set(
        experiments
          .map((e) => e.experimentId)
          .filter((id): id is string => Boolean(id))
      );

      const lastEventTime = data.experiments.length
        ? new Date(data.experiments[data.experiments.length - 1].created_at).getTime()
        : 0;

      const progress: AgentProgress = {
        registeredAt,
        experiments,
        experimentIds,
        loaded: true,
        lastEventTime,
      };

      // Merge any live events that landed while the history request was in-flight.
      const pending = this.pendingAgentExperiments.get(agentId) || [];
      for (const msg of pending) {
        this.appendToAgentProgress(progress, msg);
      }
      this.pendingAgentExperiments.delete(agentId);

      this.agentProgress.set(agentId, progress);
    } catch {
      // leave unloaded; next tab visit will retry
    }
  }

  private appendToAgentProgress(progress: AgentProgress, msg: any): boolean {
    const msgTime = msg.timestamp ? new Date(msg.timestamp).getTime() : Date.now();
    const experimentId = typeof msg.experiment_id === "string" ? msg.experiment_id : null;

    if (experimentId && progress.experimentIds.has(experimentId)) {
      return false;
    }

    const time = Math.max(0, msgTime - progress.registeredAt);
    const feasible = msg.feasible !== false;

    progress.experiments.push({
      time,
      score: msg.score,
      feasible,
      experimentId: experimentId || undefined,
    });
    if (experimentId) progress.experimentIds.add(experimentId);
    progress.lastEventTime = Math.max(progress.lastEventTime, msgTime);
    return true;
  }

  private appendAgentExperiment(msg: any) {
    if (!msg.agent_id) return;
    const progress = this.agentProgress.get(msg.agent_id);
    if (!progress || !progress.loaded) {
      const pending = this.pendingAgentExperiments.get(msg.agent_id) || [];
      pending.push(msg);
      this.pendingAgentExperiments.set(msg.agent_id, pending);
      return;
    }
    const added = this.appendToAgentProgress(progress, msg);
    if (!added) return;

    const tab = this.currentTab();
    if (tab.type === "agent" && tab.agentId === msg.agent_id) {
      this.redraw();
    }
  }

  // ── Rendering ──

  private redraw() {
    // Coalesce multiple redraw requests in the same frame. Hot paths
    // (experiment_published bursts, leaderboard sync, resize observer)
    // can fire several times per tick — without rAF batching each one
    // does an O(N) SVG rebuild.
    if (this.redrawScheduled) return;
    this.redrawScheduled = true;
    requestAnimationFrame(() => {
      this.redrawScheduled = false;
      const tab = this.currentTab();
      if (tab.type === "global") {
        this.redrawGlobal();
      } else {
        this.redrawAgent(tab.agentId, tab.agentName);
      }
    });
  }

  // Margins scale with the axis font size so the same chart code works
  // for the small dashboard panel (fs≈10) and the full-screen benchmark
  // page (fs up to 22) without y-axis labels overflowing the left edge,
  // x-axis tick labels clipping at the bottom, or rightmost agent-name
  // labels running off the right. The Math.max with the prior constants
  // preserves the original layout on small charts.
  private computeLayout() {
    const fs = axisFontPx(this.width);
    // Each margin sized to the worst case at this font size:
    //   top:    breakthrough labels are drawn at y - 8, so we need fs + 8
    //           clearance above the chart for a label sitting at y = 0.
    //   bottom: tick labels baseline at h + fs + 6, descender ~fs/4 below,
    //           plus a few px breathing room.
    //   left:   y-axis labels (text-anchor=end at x = -8) can be ~8 chars
    //           wide on log-scaled scores ("100.00M"); at ~0.55em/char
    //           that's ~4.4·fs.
    //   right:  half-strokes from end-of-data lines plus a small buffer.
    const m = {
      top: Math.max(28, fs + 12),
      right: Math.max(16, Math.round(fs * 2)),
      bottom: Math.max(28, fs + 18),
      left: Math.max(52, Math.round(fs * 5)),
    };
    const w = Math.max(0, this.width - m.left - m.right);
    const h = Math.max(0, this.height - m.top - m.bottom);
    return { m, w, h, fs };
  }

  private redrawGlobal() {
    this.g.selectAll("*").remove();

    const { m, w, h, fs } = this.computeLayout();

    if (this.globalData.length < 1) {
      // Empty-state placeholder so an unstarted challenge doesn't look
      // like a broken chart.
      this.g.append("g")
        .attr("transform", `translate(${m.left},${m.top})`)
        .append("text")
        .attr("class", "chart-empty")
        .attr("x", w / 2)
        .attr("y", h / 2)
        .attr("text-anchor", "middle")
        .attr("fill", AXIS_TEXT())
        .attr("font-size", `${fs + 2}px`)
        .attr("font-family", "var(--ui)")
        .text("No iterations yet — this challenge hasn't started");
      return;
    }

    const latestData = max(this.globalData, (d) => d.time)!;
    const xPad = Math.max(latestData * 0.15, 5000);
    const xScale = scaleLinear()
      .domain([0, latestData + xPad])
      .range([0, w]);

    const yDomain = this.getGlobalYDomain();
    if (!yDomain) return;

    const yScale = scaleLinear()
      .domain(yDomain)
      .range([h, 0]);

    const chartG = this.g.append("g")
      .attr("transform", `translate(${m.left},${m.top})`);

    const yTicks = yScale.ticks(5);
    yTicks.forEach((tick) => {
      chartG.append("line")
        .attr("x1", 0).attr("x2", w)
        .attr("y1", yScale(tick)).attr("y2", yScale(tick))
        .attr("stroke", GRID_LINE())
        .attr("stroke-width", 0.5);
    });

    const trailTime = latestData + xPad;
    for (let i = 0; i < this.globalData.length; i++) {
      const d = this.globalData[i];
      const nextX = i < this.globalData.length - 1 ? xScale(this.globalData[i + 1].time) : xScale(trailTime);
      const x0 = xScale(d.time);
      const y0 = yScale(d.score);
      const color = getAgentColor(d.agentId || d.agentName || "unknown");

      chartG.append("rect")
        .attr("x", x0)
        .attr("y", y0)
        .attr("width", Math.max(0, nextX - x0))
        .attr("height", Math.max(0, h - y0))
        .attr("fill", color)
        .attr("opacity", 0.1);

      chartG.append("line")
        .attr("x1", x0).attr("x2", nextX)
        .attr("y1", y0).attr("y2", y0)
        .attr("stroke", color)
        .attr("stroke-width", 2)
        .attr("stroke-opacity", 0.9);

      if (i < this.globalData.length - 1) {
        const nextY = yScale(this.globalData[i + 1].score);
        const nextColor = getAgentColor(this.globalData[i + 1].agentId || this.globalData[i + 1].agentName || "unknown");
        chartG.append("line")
          .attr("x1", nextX).attr("x2", nextX)
          .attr("y1", y0).attr("y2", nextY)
          .attr("stroke", nextColor)
          .attr("stroke-width", 2)
          .attr("stroke-opacity", 0.9);
      }
    }

    const breakthroughs = this.globalData
      .map((d, i) => ({ d, i }))
      .filter(({ d }) => d.isBreakthrough);
    const lastIdx = this.globalData.length - 1;
    let prevAgentKey: string | null = null;
    breakthroughs.forEach(({ d, i }) => {
      const x = xScale(d.time);
      const y = yScale(d.score);
      const color = getAgentColor(d.agentId || d.agentName || "unknown");

      chartG.append("line")
        .attr("x1", x).attr("x2", x)
        .attr("y1", 0).attr("y2", h)
        .attr("stroke", color)
        .attr("stroke-width", 0.5)
        .attr("stroke-dasharray", "3 3")
        .attr("stroke-opacity", 0.5);

      chartG.append("path")
        .attr("d", symbol(symbolDiamond, 24)())
        .attr("transform", `translate(${x},${y})`)
        .attr("fill", color)
        .attr("opacity", 0.9);

      const agentKey = d.agentId || d.agentName || null;
      const winnerChanged = agentKey !== null && agentKey !== prevAgentKey;
      const isLastPoint = i === lastIdx;
      if (d.agentName && (winnerChanged || isLastPoint)) {
        chartG.append("text")
          .attr("x", x + 6)
          .attr("y", y - 8)
          .attr("fill", color)
          .attr("font-size", `${Math.max(9, fs - 1)}px`)
          .attr("font-family", "var(--mono)")
          .attr("opacity", 0.8)
          .text(d.agentName);
      }
      prevAgentKey = agentKey;
    });

    yTicks.forEach((tick) => {
      chartG.append("text")
        .attr("x", -8)
        .attr("y", yScale(tick) + fs / 3)
        .attr("fill", AXIS_TEXT())
        .attr("font-size", `${fs}px`)
        .attr("font-family", "var(--mono)")
        .attr("text-anchor", "end")
        .text(formatScore(tick));
    });

    const xTicks = xScale.ticks(6);
    xTicks.forEach((tick) => {
      chartG.append("text")
        .attr("x", xScale(tick))
        .attr("y", h + fs + 6)
        .attr("fill", AXIS_TEXT())
        .attr("font-size", `${fs}px`)
        .attr("font-family", "var(--mono)")
        .attr("text-anchor", "middle")
        .text(formatElapsed(tick));
    });
  }

  private redrawAgent(agentId: string, agentName: string) {
    this.g.selectAll("*").remove();

    const progress = this.agentProgress.get(agentId);
    const { m, w, h, fs } = this.computeLayout();

    const chartG = this.g.append("g")
      .attr("transform", `translate(${m.left},${m.top})`);

    if (!progress || progress.experiments.length === 0) {
      chartG.append("text")
        .attr("x", w / 2)
        .attr("y", h / 2)
        .attr("fill", AXIS_TEXT())
        .attr("font-size", `${fs + 1}px`)
        .attr("font-family", "var(--ui)")
        .attr("text-anchor", "middle")
        .text(progress ? `no attempts yet from ${agentName}` : "loading…");
      return;
    }

    const color = getAgentColor(agentId);
    const exps = progress.experiments;

    // X: iteration index (0..N-1), NOT wall-clock time. A late-joining
    // agent on a wall-clock axis ends up clustered at the right edge with
    // very few horizontal pixels; iteration index lets every agent's line
    // span the full chart width regardless of when they registered.
    const xDomainEnd = Math.max(exps.length - 1, 1);
    const xScale = scaleLinear()
      .domain([0, xDomainEnd])
      .range([0, w]);

    // Y: anchor on the GLOBAL chart's domain when available so per-agent
    // tabs roughly share a visual scale, but always extend it to include
    // the agent's own min/max — otherwise an agent whose scores fall
    // outside the global-best history (e.g. infeasible attempts, or runs
    // whose scores never became the global best) gets clipped off-chart
    // and looks like a flat line.
    const globalYDomain = this.getGlobalYDomain();
    const minScore = min(exps, (d) => d.score)!;
    const maxScore = max(exps, (d) => d.score)!;
    const fallbackPad = Math.max(Math.abs(maxScore - minScore) * 0.15, 1);
    const yDomain: [number, number] = globalYDomain
      ? [
          Math.min(globalYDomain[0], minScore - fallbackPad),
          Math.max(globalYDomain[1], maxScore + fallbackPad),
        ]
      : [minScore - fallbackPad, maxScore + fallbackPad];
    const yScale = scaleLinear()
      .domain(yDomain)
      .range([h, 0]);

    const yTicks = yScale.ticks(5);
    yTicks.forEach((tick) => {
      chartG.append("line")
        .attr("x1", 0).attr("x2", w)
        .attr("y1", yScale(tick)).attr("y2", yScale(tick))
        .attr("stroke", GRID_LINE())
        .attr("stroke-width", 0.5);
    });

    // Step plot: each attempt's score is held until the next attempt.
    // X is the iteration index, so each step is exactly one unit wide.
    for (let i = 0; i < exps.length; i++) {
      const d = exps[i];
      const x0 = xScale(i);
      const y0 = yScale(d.score);
      const next = exps[i + 1];
      const xEnd = next ? xScale(i + 1) : x0;

      if (xEnd > x0) {
        chartG.append("line")
          .attr("x1", x0).attr("x2", xEnd)
          .attr("y1", y0).attr("y2", y0)
          .attr("stroke", color)
          .attr("stroke-width", 2)
          .attr("stroke-opacity", 0.9);
      }

      if (next) {
        const yNext = yScale(next.score);
        chartG.append("line")
          .attr("x1", xEnd).attr("x2", xEnd)
          .attr("y1", y0).attr("y2", yNext)
          .attr("stroke", color)
          .attr("stroke-width", 2)
          .attr("stroke-opacity", 0.9);
      }

      // Attempt marker — dimmer for infeasible so they're distinguishable.
      // Replaced with a richer event marker below when this iteration was
      // hinted with tacit knowledge / inspiration, or was the last iteration
      // on a trajectory that subsequently became inactive.
      const event = pickEventKind(d);
      if (event === "trajectory_deactivated") {
        // Cross — trajectory went into the inactive pool after this point.
        const r = 5;
        chartG.append("line")
          .attr("x1", x0 - r).attr("x2", x0 + r)
          .attr("y1", y0 - r).attr("y2", y0 + r)
          .attr("stroke", color).attr("stroke-width", 1.6).attr("opacity", 0.95);
        chartG.append("line")
          .attr("x1", x0 - r).attr("x2", x0 + r)
          .attr("y1", y0 + r).attr("y2", y0 - r)
          .attr("stroke", color).attr("stroke-width", 1.6).attr("opacity", 0.95);
      } else if (event === "tacit_knowledge") {
        // Star — agent was nudged with a tacit-knowledge hint on the prior
        // /api/state call.
        chartG.append("path")
          .attr("d", symbol(symbolStar, 60)())
          .attr("transform", `translate(${x0},${y0})`)
          .attr("fill", color).attr("opacity", 0.95)
          .attr("stroke", color).attr("stroke-width", 0.5);
      } else if (event === "inspiration") {
        // Square — agent was given another agent's code as inspiration.
        chartG.append("path")
          .attr("d", symbol(symbolSquare, 50)())
          .attr("transform", `translate(${x0},${y0})`)
          .attr("fill", color).attr("opacity", 0.95)
          .attr("stroke", color).attr("stroke-width", 0.5);
      } else {
        chartG.append("circle")
          .attr("cx", x0)
          .attr("cy", y0)
          .attr("r", 2.5)
          .attr("fill", color)
          .attr("opacity", d.feasible ? 0.9 : 0.4);
      }
    }

    // Legend for the event markers.
    this.drawAgentEventLegend(chartG, w, fs, color);

    yTicks.forEach((tick) => {
      chartG.append("text")
        .attr("x", -8)
        .attr("y", yScale(tick) + fs / 3)
        .attr("fill", AXIS_TEXT())
        .attr("font-size", `${fs}px`)
        .attr("font-family", "var(--mono)")
        .attr("text-anchor", "end")
        .text(formatScore(tick));
    });

    // Iteration-index axis: integer ticks, no time formatting.
    const xTickStep = Math.max(1, Math.ceil(xDomainEnd / 6));
    for (let t = 0; t <= xDomainEnd; t += xTickStep) {
      chartG.append("text")
        .attr("x", xScale(t))
        .attr("y", h + fs + 6)
        .attr("fill", AXIS_TEXT())
        .attr("font-size", `${fs}px`)
        .attr("font-family", "var(--mono)")
        .attr("text-anchor", "middle")
        .text(`#${t}`);
    }
  }

  private drawAgentEventLegend(
    chartG: any, chartWidth: number, fs: number, color: string,
  ) {
    // Stack short rows in the top-right corner. Each row is a tiny marker
    // followed by a label. Kept compact so it doesn't crowd the plot.
    const items: { kind: "trajectory_deactivated" | "tacit_knowledge" | "inspiration"; label: string }[] = [
      { kind: "trajectory_deactivated", label: "trajectory deactivated" },
      { kind: "tacit_knowledge",        label: "tacit knowledge" },
      { kind: "inspiration",            label: "inspiration" },
    ];
    const lineH = Math.max(12, fs + 2);
    // "trajectory deactivated" is the longest label (~21 chars). Reserve
    // ~0.6em per char so the legend doesn't run off the right edge once
    // fs grows on the full-screen benchmark page.
    const x0 = chartWidth - Math.max(130, Math.round(fs * 13));
    let y0 = 4;
    const legend = chartG.append("g").attr("class", "agent-event-legend");
    items.forEach((item) => {
      const cy = y0 + lineH / 2;
      if (item.kind === "trajectory_deactivated") {
        const r = 4;
        legend.append("line")
          .attr("x1", x0 - r).attr("x2", x0 + r)
          .attr("y1", cy - r).attr("y2", cy + r)
          .attr("stroke", color).attr("stroke-width", 1.4);
        legend.append("line")
          .attr("x1", x0 - r).attr("x2", x0 + r)
          .attr("y1", cy + r).attr("y2", cy - r)
          .attr("stroke", color).attr("stroke-width", 1.4);
      } else if (item.kind === "tacit_knowledge") {
        legend.append("path")
          .attr("d", symbol(symbolStar, 36)())
          .attr("transform", `translate(${x0},${cy})`)
          .attr("fill", color).attr("opacity", 0.9);
      } else {
        legend.append("path")
          .attr("d", symbol(symbolSquare, 30)())
          .attr("transform", `translate(${x0},${cy})`)
          .attr("fill", color).attr("opacity", 0.9);
      }
      legend.append("text")
        .attr("x", x0 + 10)
        .attr("y", cy + fs / 3)
        .attr("fill", AXIS_TEXT())
        .attr("font-size", `${Math.max(9, fs - 2)}px`)
        .attr("font-family", "var(--ui)")
        .text(item.label);
      y0 += lineH;
    });
  }

  private getGlobalYDomain(): [number, number] | null {
    if (this.globalData.length < 1) return null;
    const scoreMin = min(this.globalData, (d) => d.score);
    const scoreMax = max(this.globalData, (d) => d.score);
    if (scoreMin == null || scoreMax == null) return null;

    const pad = Math.max(Math.abs(scoreMax - scoreMin) * 0.15, 1);
    const yMin = Math.max(1, scoreMin - pad);
    const yMax = scoreMax + pad;
    return [yMin, yMax];
  }
}

function pickEventKind(
  e: AgentExperiment,
): "trajectory_deactivated" | "tacit_knowledge" | "inspiration" | null {
  // Priority: a trajectory deactivation is the loudest event, so it wins
  // when both apply (rare — the agent published an iteration that was then
  // the last on a trajectory which became inactive on its next /api/state
  // call). Hint markers come next.
  if (e.trajectoryDeactivated) return "trajectory_deactivated";
  if (e.receivedHint === "tacit_knowledge") return "tacit_knowledge";
  if (e.receivedHint === "inspiration") return "inspiration";
  return null;
}

function formatElapsed(ms: number): string {
  const totalSec = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}
