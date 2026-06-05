import { max, min } from "d3-array";
import { scaleLinear } from "d3-scale";
import { select } from "d3-selection";
import { symbol, symbolDiamond, symbolSquare, symbolStar } from "d3-shape";
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform, type D3ZoomEvent } from "d3-zoom";
import { getAgentColor, token } from "../lib/colors";
import { formatScore } from "../lib/format";
import { isBetter } from "../lib/swarmConfig";
import { AgentProgressStore, type AgentExperiment } from "./agentProgressStore";
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

  private progressStore = new AgentProgressStore();

  private tabLabelEl!: HTMLElement;
  private tabPrevEl!: HTMLElement;
  private tabNextEl!: HTMLElement;
  private zoomResetEl!: HTMLElement;
  private redrawScheduled = false;

  // X-axis pan/zoom. The transform is applied to the x-scale on every redraw
  // (drag = scroll along a long run, wheel = zoom into a span). Reset to
  // identity on tab/challenge switch so a fresh dataset always starts fit.
  private zoomBehavior!: ZoomBehavior<SVGSVGElement, unknown>;
  private xZoomTransform: ZoomTransform = zoomIdentity;

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="panel-inner chart-panel">
        <div class="panel-label">BENCHMARK PROGRESS</div>
        <div class="chart-tabs" id="chart-tabs">
          <button class="chart-tab-btn" id="chart-tab-prev" type="button">&lsaquo;</button>
          <span class="chart-tab-label" id="chart-tab-label">GLOBAL</span>
          <button class="chart-tab-btn" id="chart-tab-next" type="button">&rsaquo;</button>
          <button class="chart-zoom-reset" id="chart-zoom-reset" type="button" title="Reset zoom (or double-click the chart)" style="display:none">⟲ reset zoom</button>
        </div>
        <svg id="chart-svg"></svg>
      </div>
    `;

    this.tabLabelEl = document.getElementById("chart-tab-label")!;
    this.tabPrevEl = document.getElementById("chart-tab-prev")!;
    this.tabNextEl = document.getElementById("chart-tab-next")!;

    this.tabPrevEl.addEventListener("click", () => this.cycleTab(-1));
    this.tabNextEl.addEventListener("click", () => this.cycleTab(1));

    this.zoomResetEl = document.getElementById("chart-zoom-reset")!;
    this.zoomResetEl.addEventListener("click", () => this.resetZoom());

    // Measure the SVG itself, not the parent panel — the SVG is `flex: 1`
    // so the browser has already sized it to fit the remaining space after
    // the panel label, tabs row, and panel padding. The previous
    // `parent.height - 48` underestimated the chrome (closer to ~78px on
    // the mainpage), so the SVG coordinate space extended below the
    // visible flex box and the bottom-most y-tick label got clipped.
    const svgEl = document.getElementById("chart-svg")!;
    const rect = svgEl.getBoundingClientRect();
    this.width = rect.width;
    this.height = rect.height;

    this.svg = select("#chart-svg")
      .attr("width", this.width)
      .attr("height", this.height);

    this.g = this.svg.append("g");

    // X-axis pan/zoom. scaleExtent floor of 1 means you can't zoom out past
    // "fit" (no empty gutters); ceiling lets long runs be expanded ~64× to
    // read a dense segment. We apply the resulting transform to the x-scale in
    // redraw rather than transforming the SVG group, so y-scale, axis labels
    // and stroke widths stay unscaled.
    this.zoomBehavior = zoom<SVGSVGElement, unknown>()
      .scaleExtent([1, 64])
      .on("zoom", (e: D3ZoomEvent<SVGSVGElement, unknown>) => {
        this.xZoomTransform = e.transform;
        this.zoomResetEl.style.display = e.transform.k > 1.001 ? "" : "none";
        this.redraw();
      });
    this.svg.call(this.zoomBehavior);
    // Replace d3-zoom's default double-click-to-zoom-in with reset-to-fit.
    this.svg.on("dblclick.zoom", null);
    this.svg.on("dblclick", () => this.resetZoom());

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
      const newRect = svgEl.getBoundingClientRect();
      this.width = newRect.width;
      this.height = newRect.height;
      this.svg.attr("width", this.width).attr("height", this.height);
      this.redraw();
    });
    observer.observe(svgEl);

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
    this.resetZoomSilently();
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
      this.progressStore.clear();
      this.tabs = [{ type: "global" }];
      this.currentTabIndex = 0;
      this.resetZoomSilently();
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
    this.resetZoomSilently();
    this.renderTabLabel();
    const tab = this.currentTab();
    if (tab.type === "agent") {
      this.progressStore.load(this.apiUrl, tab.agentId).then(() => {
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
  // Cache + fetch + pending-merge lives in AgentProgressStore. The chart
  // only needs to (a) trigger a load when an agent tab is opened, and
  // (b) feed live events in. Redraw decisions stay here because they depend
  // on which tab is currently visible.

  private appendAgentExperiment(msg: { agent_id?: string }): void {
    const added = this.progressStore.appendLive(msg);
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
    //   left:   y-axis labels (text-anchor=end at x = -8) can run up to
    //           ~9 chars on negative log-magnitude scores ("-100.00M");
    //           at ~0.6em/char that's ~5.4·fs. The previous 5.0·fs / 52px
    //           floor sized for the positive-only case and clipped the
    //           leading minus sign on the small mainpage chart.
    //   right:  half-strokes from end-of-data lines plus a small buffer.
    const m = {
      top: Math.max(28, fs + 12),
      right: Math.max(16, Math.round(fs * 2)),
      bottom: Math.max(28, fs + 18),
      left: Math.max(60, Math.round(fs * 6)),
    };
    const w = Math.max(0, this.width - m.left - m.right);
    const h = Math.max(0, this.height - m.top - m.bottom);
    return { m, w, h, fs };
  }

  private redrawGlobal() {
    this.g.selectAll("*").remove();

    const { m, w, h, fs } = this.computeLayout();
    this.configureZoomExtent();

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
    const baseXScale = scaleLinear()
      .domain([0, latestData + xPad])
      .range([0, w]);
    // Apply the current pan/zoom to the x-axis only.
    const xScale = this.xZoomTransform.rescaleX(baseXScale);

    const yDomain = this.getGlobalYDomain();
    if (!yDomain) return;

    const yScale = scaleLinear()
      .domain(yDomain)
      .range([h, 0]);

    const chartG = this.g.append("g")
      .attr("transform", `translate(${m.left},${m.top})`);
    // Data marks go in a clipped sub-group so panning/zooming never draws them
    // over the axes or into the margins; gridlines and axis labels stay in
    // chartG (unclipped).
    const plotG = this.appendClippedPlot(chartG, w, h);

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

      plotG.append("rect")
        .attr("x", x0)
        .attr("y", y0)
        .attr("width", Math.max(0, nextX - x0))
        .attr("height", Math.max(0, h - y0))
        .attr("fill", color)
        .attr("opacity", 0.1);

      plotG.append("line")
        .attr("x1", x0).attr("x2", nextX)
        .attr("y1", y0).attr("y2", y0)
        .attr("stroke", color)
        .attr("stroke-width", 2)
        .attr("stroke-opacity", 0.9);

      if (i < this.globalData.length - 1) {
        const nextY = yScale(this.globalData[i + 1].score);
        const nextColor = getAgentColor(this.globalData[i + 1].agentId || this.globalData[i + 1].agentName || "unknown");
        plotG.append("line")
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

      plotG.append("line")
        .attr("x1", x).attr("x2", x)
        .attr("y1", 0).attr("y2", h)
        .attr("stroke", color)
        .attr("stroke-width", 0.5)
        .attr("stroke-dasharray", "3 3")
        .attr("stroke-opacity", 0.5);

      plotG.append("path")
        .attr("d", symbol(symbolDiamond, 24)())
        .attr("transform", `translate(${x},${y})`)
        .attr("fill", color)
        .attr("opacity", 0.9);

      const agentKey = d.agentId || d.agentName || null;
      const winnerChanged = agentKey !== null && agentKey !== prevAgentKey;
      const isLastPoint = i === lastIdx;
      // Labels render unclipped (they may overflow the right margin, as before)
      // but are culled when panned outside the plot so they don't float in the
      // y-axis gutter.
      if (d.agentName && (winnerChanged || isLastPoint) && x >= 0 && x <= w) {
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
    const [vis0, vis1] = xScale.domain();
    const fmtElapsed = makeElapsedFormatter(Math.max(1, vis1 - vis0));
    xTicks.forEach((tick) => {
      chartG.append("text")
        .attr("x", xScale(tick))
        .attr("y", h + fs + 6)
        .attr("fill", AXIS_TEXT())
        .attr("font-size", `${fs}px`)
        .attr("font-family", "var(--mono)")
        .attr("text-anchor", "middle")
        .text(fmtElapsed(tick));
    });
  }

  private redrawAgent(agentId: string, agentName: string) {
    this.g.selectAll("*").remove();

    const progress = this.progressStore.get(agentId);
    const { m, w, h, fs } = this.computeLayout();
    this.configureZoomExtent();

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
    const baseXScale = scaleLinear()
      .domain([0, xDomainEnd])
      .range([0, w]);
    const xScale = this.xZoomTransform.rescaleX(baseXScale);

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

    const plotG = this.appendClippedPlot(chartG, w, h);

    // Step plot: each attempt's score is held until the next attempt.
    // X is the iteration index, so each step is exactly one unit wide.
    for (let i = 0; i < exps.length; i++) {
      const d = exps[i];
      const x0 = xScale(i);
      const y0 = yScale(d.score);
      const next = exps[i + 1];
      const xEnd = next ? xScale(i + 1) : x0;

      if (xEnd > x0) {
        plotG.append("line")
          .attr("x1", x0).attr("x2", xEnd)
          .attr("y1", y0).attr("y2", y0)
          .attr("stroke", color)
          .attr("stroke-width", 2)
          .attr("stroke-opacity", 0.9);
      }

      if (next) {
        const yNext = yScale(next.score);
        plotG.append("line")
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
        plotG.append("line")
          .attr("x1", x0 - r).attr("x2", x0 + r)
          .attr("y1", y0 - r).attr("y2", y0 + r)
          .attr("stroke", color).attr("stroke-width", 1.6).attr("opacity", 0.95);
        plotG.append("line")
          .attr("x1", x0 - r).attr("x2", x0 + r)
          .attr("y1", y0 + r).attr("y2", y0 - r)
          .attr("stroke", color).attr("stroke-width", 1.6).attr("opacity", 0.95);
      } else if (event === "tacit_knowledge") {
        // Star — agent was nudged with a tacit-knowledge hint on the prior
        // /api/state call.
        plotG.append("path")
          .attr("d", symbol(symbolStar, 60)())
          .attr("transform", `translate(${x0},${y0})`)
          .attr("fill", color).attr("opacity", 0.95)
          .attr("stroke", color).attr("stroke-width", 0.5);
      } else if (event === "inspiration") {
        // Square — agent was given another agent's code as inspiration.
        plotG.append("path")
          .attr("d", symbol(symbolSquare, 50)())
          .attr("transform", `translate(${x0},${y0})`)
          .attr("fill", color).attr("opacity", 0.95)
          .attr("stroke", color).attr("stroke-width", 0.5);
      } else {
        plotG.append("circle")
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

    // Iteration-index axis: integer ticks within the (possibly zoomed) visible
    // window, no time formatting. Dedupe so a zoomed-in span doesn't repeat the
    // same rounded index.
    const seenTicks = new Set<number>();
    xScale.ticks(6).forEach((t) => {
      const idx = Math.round(t);
      if (idx < 0 || idx > xDomainEnd || seenTicks.has(idx)) return;
      seenTicks.add(idx);
      chartG.append("text")
        .attr("x", xScale(idx))
        .attr("y", h + fs + 6)
        .attr("fill", AXIS_TEXT())
        .attr("font-size", `${fs}px`)
        .attr("font-family", "var(--mono)")
        .attr("text-anchor", "middle")
        .text(`#${idx}`);
    });
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

    // Pad both sides symmetrically. Previously yMin was clamped to >= 1
    // because the y-axis used scaleLog, but now that we always use
    // scaleLinear that floor inverts the domain whenever all scores are
    // negative (e.g. job_scheduling at -4k..-2k) — every point ends up
    // rendered below the visible chart.
    const pad = Math.max(Math.abs(scoreMax - scoreMin) * 0.15, 1);
    return [scoreMin - pad, scoreMax + pad];
  }

  // Build a sub-group clipped to the [0,w]×[0,h] plot rect. Data marks go here
  // so pan/zoom never paints them over the axes or into the margins.
  private appendClippedPlot(chartG: any, w: number, h: number): any {
    chartG.append("clipPath").attr("id", "chart-plot-clip")
      .append("rect").attr("x", 0).attr("y", 0).attr("width", w).attr("height", h);
    return chartG.append("g").attr("clip-path", "url(#chart-plot-clip)");
  }

  // Bound the zoom gesture to the current SVG box so you can't pan/zoom out
  // into empty space (paired with the scaleExtent floor of 1).
  private configureZoomExtent() {
    if (!this.zoomBehavior) return;
    this.zoomBehavior
      .extent([[0, 0], [this.width, this.height]])
      .translateExtent([[0, 0], [this.width, this.height]]);
  }

  // User-triggered reset (button / double-click): fires a zoom event so the
  // chart redraws back at fit.
  private resetZoom() {
    if (!this.zoomBehavior) return;
    this.zoomBehavior.transform(this.svg, zoomIdentity);
  }

  // Silent reset on tab/challenge switch — clears the transform without firing
  // a zoom event, since those code paths already redraw.
  private resetZoomSilently() {
    this.xZoomTransform = zoomIdentity;
    const node = this.svg?.node?.() as any;
    if (node) node.__zoom = zoomIdentity;
    if (this.zoomResetEl) this.zoomResetEl.style.display = "none";
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

// Pick a tick-formatter for elapsed-time x-axes based on the total span
// the axis covers. Returns a closure so every tick is formatted in the
// same unit family — picking per-tick would mix units (e.g. "59:00" and
// "1h00m") on one axis and look bad.
//
//   span < 1 min   → "Xs"           (e.g. "30s")
//   span < 1 hour  → "M:SS"         (e.g. "12:30")
//   span < 1 day   → "Hh Mm" / "Hh" (e.g. "2h30m", "6h")
//   span ≥ 1 day   → "Dd Hh" / "Dd" (e.g. "1d6h", "3d")
//
// Without this, a swarm running for days renders ticks as "4320:00",
// "4500:00", etc. — minutes-only, requiring the viewer to divide by 1440
// in their head to recover "3 days, 3 days 2 hours, ...".
function makeElapsedFormatter(domainMs: number): (ms: number) => string {
  const SEC = 1000;
  const MIN = 60 * SEC;
  const HOUR = 60 * MIN;
  const DAY = 24 * HOUR;

  if (domainMs < MIN) {
    return (ms) => `${Math.max(0, Math.round(ms / SEC))}s`;
  }
  if (domainMs < HOUR) {
    return (ms) => {
      const totalSec = Math.max(0, Math.floor(ms / SEC));
      const m = Math.floor(totalSec / 60);
      const s = totalSec % 60;
      return `${m}:${s.toString().padStart(2, "0")}`;
    };
  }
  if (domainMs < DAY) {
    return (ms) => {
      const totalMin = Math.max(0, Math.floor(ms / MIN));
      const h = Math.floor(totalMin / 60);
      const m = totalMin % 60;
      return m === 0 ? `${h}h` : `${h}h${m}m`;
    };
  }
  return (ms) => {
    const totalHr = Math.max(0, Math.floor(ms / HOUR));
    const d = Math.floor(totalHr / 24);
    const h = totalHr % 24;
    return h === 0 ? `${d}d` : `${d}d${h}h`;
  };
}
