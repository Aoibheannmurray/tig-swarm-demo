import { max, min } from "d3-array";
import { scaleLinear, scaleSymlog } from "d3-scale";
import { select } from "d3-selection";
import { symbol, symbolDiamond, symbolSquare, symbolStar } from "d3-shape";
import {
  clampZoom, identityZoom, isZoomed, panBy, panByThumb, panToThumbStart,
  thumbGeometry, zoomAt, type AxisZoom,
} from "../lib/axisZoom";
import { getAgentColor, token } from "../lib/colors";
import { formatScore } from "../lib/format";
import { getDashboardUrls } from "../lib/bootstrap";
import { isBetter } from "../lib/swarmConfig";
import { isComparable } from "../lib/mainnetBaseline";
import { AgentProgressStore, type AgentExperiment } from "./agentProgressStore";
import type { MainnetBaseline, Panel, WSMessage } from "../types";

const AXIS_TEXT = () => token("--ink-dim", "rgba(26,26,26,0.50)");
const GRID_LINE = () => token("--border-subtle", "rgba(26,26,26,0.08)");

// Axis font scales with chart width so /benchmark.html (full-screen, ~1600px+)
// gets readable axis labels while the multi-panel home grid (~600px) stays
// compact. Clamps to [10, 22] px so it never goes microscopic on a phone or
// gigantic on an ultrawide.
const axisFontPx = (width: number) =>
  Math.min(22, Math.max(10, Math.round(width / 90)));

// Tooltip for the zoom hint chip — the one place the gesture vocabulary is
// spelled out for the user.
const ZOOM_HELP = [
  "Wheel: zoom both axes",
  "Shift+wheel: time only",
  "Alt+wheel: score only",
  "Wheel over an axis: that axis only",
  "Drag to pan, or drag the scrollbars",
  "Double-click to reset",
].join(" · ");

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

// Cap on retained global best-so-far points. globalData only grows on a new
// global best, so this is a high ceiling in practice — but a swarm running for
// weeks can still cross it, and every redraw is an O(N) SVG rebuild. When we
// exceed it we drop the OLDEST points: the curve is a monotonic best-so-far
// line, so the oldest points are the lowest scores (least interesting) and the
// recent high-score region — the part users care about — is preserved.
const MAX_GLOBAL_POINTS = 2000;

export class ChartPanel implements Panel {
  private svg!: any;
  private g!: any;
  private globalData: DataPoint[] = [];
  // The mainnet threshold for the viewed challenge (null until /api/state
  // reports one). Survives `reset`: it belongs to the challenge, not the run.
  private baseline: MainnetBaseline | null = null;
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
  private zoomHintEl!: HTMLElement;
  private yScaleToggleEl!: HTMLElement;
  private scrollX!: { track: HTMLElement; thumb: HTMLElement };
  private scrollY!: { track: HTMLElement; thumb: HTMLElement };
  private redrawScheduled = false;

  // Y-axis scale. "log" is a symlog scale (log-like away from zero, linear
  // through it) because scores can be negative — neuralnet divergence runs
  // sit at -2M while the best band is +500k, and a plain scaleLog inverts
  // the domain on all-negative challenges (that bug shipped once already).
  // Persisted so a host watching the benchmark page keeps their choice.
  private yScaleMode: "linear" | "log" =
    (localStorage.getItem("chartYScaleMode") as "linear" | "log") || "log";

  // Pan/zoom. The two axes zoom INDEPENDENTLY: a run that spans days but
  // whose interesting scores sit in a 2k-wide band needs the y-axis stretched
  // far harder than the x-axis, which a single shared transform can't express.
  // Each axis keeps its own {k, t} (see lib/axisZoom) and both are applied to
  // their own scale on every redraw. Reset to fit on tab/challenge switch so a
  // fresh dataset always starts unzoomed.
  private zx: AxisZoom = identityZoom();
  private zy: AxisZoom = identityZoom();

  // The plot box (inside the axis margins) from the last layout, in SVG
  // pixels. Pointer handlers need it to turn a client coordinate into an
  // anchor and to decide which axis a gesture belongs to.
  private plot = { left: 0, top: 0, w: 0, h: 0 };

  // Live pointers on the SVG, keyed by pointerId: one = drag-to-pan, two =
  // pinch (each axis scaled by its own finger span, so a pinch can stretch
  // one axis alone). `dragAxes` is which axes the current drag moves.
  private pointers = new Map<number, { x: number; y: number }>();
  private dragAxes: { x: boolean; y: boolean } | null = null;
  private pinchSpan: { dx: number; dy: number } | null = null;

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="panel-inner chart-panel">
        <div class="panel-label">BENCHMARK PROGRESS</div>
        <div class="chart-tabs" id="chart-tabs">
          <button class="chart-tab-btn" id="chart-tab-prev" type="button">&lsaquo;</button>
          <span class="chart-tab-label" id="chart-tab-label">GLOBAL</span>
          <button class="chart-tab-btn" id="chart-tab-next" type="button">&rsaquo;</button>
          <button class="chart-zoom-reset" id="chart-yscale-toggle" type="button" title="Toggle y-axis scale (log handles negative scores)"></button>
          <span class="chart-zoom-hint" id="chart-zoom-hint" title="${ZOOM_HELP}">shift+wheel: x · alt+wheel: y</span>
          <button class="chart-zoom-reset" id="chart-zoom-reset" type="button" title="Reset zoom (or double-click the chart)" style="display:none">⟲ reset zoom</button>
        </div>
        <div class="chart-plot-wrap">
          <svg id="chart-svg"></svg>
          <div class="chart-scroll chart-scroll-y" id="chart-scroll-y">
            <div class="chart-scroll-thumb" id="chart-scroll-y-thumb"></div>
          </div>
          <div class="chart-scroll chart-scroll-x" id="chart-scroll-x">
            <div class="chart-scroll-thumb" id="chart-scroll-x-thumb"></div>
          </div>
        </div>
      </div>
    `;

    this.tabLabelEl = document.getElementById("chart-tab-label")!;
    this.tabPrevEl = document.getElementById("chart-tab-prev")!;
    this.tabNextEl = document.getElementById("chart-tab-next")!;

    this.tabPrevEl.addEventListener("click", () => this.cycleTab(-1));
    this.tabNextEl.addEventListener("click", () => this.cycleTab(1));

    this.zoomResetEl = document.getElementById("chart-zoom-reset")!;
    this.zoomResetEl.addEventListener("click", () => this.resetZoom());
    this.zoomHintEl = document.getElementById("chart-zoom-hint")!;

    this.scrollX = {
      track: document.getElementById("chart-scroll-x")!,
      thumb: document.getElementById("chart-scroll-x-thumb")!,
    };
    this.scrollY = {
      track: document.getElementById("chart-scroll-y")!,
      thumb: document.getElementById("chart-scroll-y-thumb")!,
    };
    this.installScrollbar("x");
    this.installScrollbar("y");

    this.yScaleToggleEl = document.getElementById("chart-yscale-toggle")!;
    this.renderYScaleToggle();
    this.yScaleToggleEl.addEventListener("click", () => {
      this.yScaleMode = this.yScaleMode === "log" ? "linear" : "log";
      localStorage.setItem("chartYScaleMode", this.yScaleMode);
      this.renderYScaleToggle();
      this.redraw();
    });

    // Measure the SVG itself, not the parent panel — the SVG fills its grid
    // cell in `.chart-plot-wrap`, so the browser has already sized it to what
    // is left after the panel label, tabs row, scrollbar gutters and padding.
    // The previous `parent.height - 48` underestimated the chrome (closer to
    // ~78px on the mainpage), so the SVG coordinate space extended below the
    // visible box and the bottom-most y-tick label got clipped.
    const svgEl = document.getElementById("chart-svg")!;
    const rect = svgEl.getBoundingClientRect();
    this.width = rect.width;
    this.height = rect.height;

    this.svg = select("#chart-svg")
      .attr("width", this.width)
      .attr("height", this.height);

    this.g = this.svg.append("g");

    // Prime the plot box so a gesture arriving before the first redraw has
    // real margins to work with.
    this.computeLayout();

    // Pan/zoom gestures. The transforms are applied to the scales in redraw
    // rather than to the SVG group, so axis labels and stroke widths stay
    // unscaled at any zoom depth.
    this.installGestures(svgEl as unknown as SVGSVGElement);

    this.apiUrl = getDashboardUrls().apiUrl;

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
    this.globalData =
      filtered.length > MAX_GLOBAL_POINTS
        ? filtered.slice(filtered.length - MAX_GLOBAL_POINTS)
        : filtered;
    if (this.currentTab().type === "global") this.redraw();
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "mainnet_baseline") {
      this.baseline = msg.baseline;
      this.redraw();
      return;
    }
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
      // Bound retained points (see MAX_GLOBAL_POINTS) — drop the oldest.
      if (this.globalData.length > MAX_GLOBAL_POINTS) {
        this.globalData.splice(0, this.globalData.length - MAX_GLOBAL_POINTS);
      }
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
    // Remember the plot box for the pointer handlers, and re-clamp: a resize
    // changes how far a given zoom level is allowed to pan.
    this.plot = { left: m.left, top: m.top, w, h };
    this.zx = clampZoom(this.zx, w);
    this.zy = clampZoom(this.zy, h);
    this.syncZoomChrome();
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
    const baseXScale = scaleLinear()
      .domain([0, latestData + xPad])
      .range([0, w]);
    // Apply each axis' own pan/zoom.
    const xScale = this.rescaleX(baseXScale, w);

    const yDomain = this.getGlobalYDomain();
    if (!yDomain) return;

    const yScale = this.rescaleY(this.makeYScale(yDomain, h), h);

    const chartG = this.g.append("g")
      .attr("transform", `translate(${m.left},${m.top})`);
    // Data marks go in a clipped sub-group so panning/zooming never draws them
    // over the axes or into the margins; gridlines and axis labels stay in
    // chartG (unclipped).
    const plotG = this.appendClippedPlot(chartG, w, h);

    const yTicks = this.yTicksFor(yScale, h);
    yTicks.forEach((tick) => {
      chartG.append("line")
        .attr("x1", 0).attr("x2", w)
        .attr("y1", yScale(tick)).attr("y2", yScale(tick))
        .attr("stroke", GRID_LINE())
        .attr("stroke-width", 0.5);
    });

    this.drawBaseline(chartG, yScale, w, fs);

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
      // axis gutters — including vertically, which a deep y-only zoom makes
      // easy to hit: the point scrolls off the plot but its label would still
      // be drawn over the x-axis tick labels.
      if (d.agentName && (winnerChanged || isLastPoint)
          && x >= 0 && x <= w && y >= 0 && y <= h) {
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
    const xScale = this.rescaleX(baseXScale, w);

    // Y: anchor on the GLOBAL chart's domain when available so per-agent
    // tabs roughly share a visual scale, but always extend it to include
    // the agent's own min/max — otherwise an agent whose scores fall
    // outside the global-best history (e.g. infeasible attempts, or runs
    // whose scores never became the global best) gets clipped off-chart
    // and looks like a flat line.
    const globalYDomain = this.getGlobalYDomain();
    const minScore = min(exps, (d) => d.score)!;
    const maxScore = max(exps, (d) => d.score)!;
    const agentDomain = this.padYDomain(minScore, maxScore);
    const yDomain: [number, number] = globalYDomain
      ? [
          Math.min(globalYDomain[0], agentDomain[0]),
          Math.max(globalYDomain[1], agentDomain[1]),
        ]
      : agentDomain;
    const yScale = this.rescaleY(this.makeYScale(yDomain, h), h);

    const yTicks = this.yTicksFor(yScale, h);
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
    let scoreMin = min(this.globalData, (d) => d.score);
    let scoreMax = max(this.globalData, (d) => d.score);
    if (scoreMin == null || scoreMax == null) return null;
    // Keep the mainnet line inside the view. A threshold the swarm hasn't
    // reached yet sits above every plotted point, and a line drawn off the top
    // of the chart is worse than no line — the one thing it has to show is how
    // far away it is.
    const bar = this.baselineScore();
    if (bar !== null) {
      scoreMin = Math.min(scoreMin, bar);
      scoreMax = Math.max(scoreMax, bar);
    }
    return this.padYDomain(scoreMin, scoreMax);
  }

  // The mainnet score, only when it can honestly be drawn on this axis.
  private baselineScore(): number | null {
    return isComparable(this.baseline) ? this.baseline!.score : null;
  }

  // The dashed mainnet threshold plus its label. Drawn into the UNCLIPPED
  // group with the gridlines: it is a reference like an axis, not a data mark,
  // so it should span the full plot width and keep its label readable at the
  // edge even when the user has panned the data away.
  private drawBaseline(chartG: any, yScale: any, w: number, fs: number): void {
    const bar = this.baselineScore();
    if (bar === null) return;
    const y = yScale(bar);
    // Panned out of view — drawing it pinned to an edge would misrepresent
    // where the bar actually sits.
    if (!Number.isFinite(y) || y < 0 || y > yScale.range()[0]) return;

    const accent = token("--color-accent", "#c2410c");
    chartG.append("line")
      .attr("class", "chart-mainnet-line")
      .attr("x1", 0).attr("x2", w)
      .attr("y1", y).attr("y2", y)
      .attr("stroke", accent)
      .attr("stroke-width", 1.5)
      .attr("stroke-dasharray", "7 5")
      .attr("opacity", 0.85);

    const b = this.baseline!;
    const label = `mainnet · ${b.algorithm ?? "?"} · ${formatScore(bar)}`;
    // Above the line normally; below it when the line is near the top, so the
    // text never escapes the plot.
    const above = y > fs + 8;
    const text = chartG.append("text")
      .attr("class", "chart-mainnet-label")
      .attr("x", w - 4)
      .attr("y", above ? y - 6 : y + fs + 4)
      .attr("text-anchor", "end")
      .attr("fill", accent)
      .attr("font-size", `${Math.max(9, fs - 1)}px`)
      .attr("font-family", "var(--mono, monospace)")
      .text(label);
    // A readable halo: the line and the best-so-far curve both run underneath.
    text.attr("paint-order", "stroke")
      .attr("stroke", token("--bg-page", "#fff"))
      .attr("stroke-width", 3)
      .attr("stroke-linejoin", "round");
  }

  // Pad the y-domain in the active scale's own space. Linear mode pads
  // symmetrically in score units (never clamp the floor to >= 1 — that
  // inverts the domain on all-negative challenges like job_scheduling at
  // -4k..-2k, a bug that shipped once already). Log mode pads in symlog
  // space instead: a linear pad below a positive floor (e.g. 48k - 15%
  // of a 550k range) flips the floor negative, and on a symlog axis that
  // wastes half the chart on an empty sign change.
  private padYDomain(scoreMin: number, scoreMax: number): [number, number] {
    if (this.yScaleMode === "log") {
      const c = symlogConstant(scoreMin, scoreMax);
      const t = (v: number) => Math.sign(v) * Math.log1p(Math.abs(v) / c);
      const ti = (u: number) => Math.sign(u) * c * Math.expm1(Math.abs(u));
      const lo = t(scoreMin);
      const hi = t(scoreMax);
      const pad = Math.max((hi - lo) * 0.05, 0.05);
      return [ti(lo - pad), ti(hi + pad)];
    }
    const pad = Math.max(Math.abs(scoreMax - scoreMin) * 0.15, 1);
    return [scoreMin - pad, scoreMax + pad];
  }

  // Y-scale for the active mode. "log" is symlog so negative scores (and a
  // domain crossing zero) render without the scaleLog domain-inversion trap.
  // The constant anchors where the scale transitions from linear to log-like;
  // tying it to the data's magnitude keeps the axis from wasting space on
  // |score| decades the data never visits (with the d3 default of 1, a
  // -2M..+600k domain spends ~60% of its pixels on the empty |v| < 10k band).
  private makeYScale(domain: [number, number], h: number): any {
    if (this.yScaleMode === "log") {
      return scaleSymlog()
        .domain(domain)
        .range([h, 0])
        .constant(symlogConstant(domain[0], domain[1]));
    }
    return scaleLinear().domain(domain).range([h, 0]);
  }

  // Gridline/label positions for the active mode. d3's symlog ticks() are
  // linearly spaced VALUES, which bunch into one end of a log-like axis —
  // so in log mode we instead take evenly spaced PIXEL rows and snap each
  // to a nice number. The snap precision comes from the gap to the
  // neighboring row, NOT from the value's own magnitude: when zoomed deep
  // into a narrow band (e.g. 589k..593k) magnitude-based snapping collapses
  // every row to the same 500k and the axis goes blank.
  private yTicksFor(yScale: any, h: number): number[] {
    if (this.yScaleMode === "linear") return yScale.ticks(5);
    const ticks = new Set<number>();
    const n = 5;
    for (let i = 0; i <= n; i++) {
      const v = yScale.invert((h * i) / n);
      const neighbor = yScale.invert((h * (i === n ? i - 1 : i + 1)) / n);
      ticks.add(snapToStep(v, Math.abs(neighbor - v)));
    }
    // Snapping can land a tick just outside the visible domain; cull so
    // gridlines never draw into the margins.
    return [...ticks].filter((v) => {
      const y = yScale(v);
      return y >= -0.5 && y <= h + 0.5;
    });
  }

  private renderYScaleToggle() {
    this.yScaleToggleEl.textContent =
      this.yScaleMode === "log" ? "y: log" : "y: linear";
  }

  // Build a sub-group clipped to the [0,w]×[0,h] plot rect. Data marks go here
  // so pan/zoom never paints them over the axes or into the margins.
  private appendClippedPlot(chartG: any, w: number, h: number): any {
    chartG.append("clipPath").attr("id", "chart-plot-clip")
      .append("rect").attr("x", 0).attr("y", 0).attr("width", w).attr("height", h);
    return chartG.append("g").attr("clip-path", "url(#chart-plot-clip)");
  }

  // ── Pan/zoom ──
  //
  // Each axis is rescaled from its OWN transform, which is what makes the two
  // independent: `rescaleX` mirrors d3-zoom's rescaleX for the x transform
  // alone, `rescaleY` likewise (and works on symlog, since it only goes
  // through the scale's own invert).

  private rescaleX(base: any, w: number): any {
    const { k, t } = this.zx;
    if (k === 1 && t === 0) return base;
    return base.copy().domain([base.invert(-t / k), base.invert((w - t) / k)]);
  }

  private rescaleY(base: any, h: number): any {
    const { k, t } = this.zy;
    if (k === 1 && t === 0) return base;
    // The y range runs [h, 0] (top = high score), so the domain floor comes
    // from the BOTTOM pixel.
    return base.copy().domain([base.invert((h - t) / k), base.invert(-t / k)]);
  }

  private installGestures(svgEl: SVGSVGElement) {
    svgEl.addEventListener("wheel", (e) => this.onWheel(e), { passive: false });
    svgEl.addEventListener("pointerdown", (e) => this.onPointerDown(e));
    svgEl.addEventListener("pointermove", (e) => this.onPointerMove(e));
    svgEl.addEventListener("pointerup", (e) => this.onPointerUp(e));
    svgEl.addEventListener("pointercancel", (e) => this.onPointerUp(e));
    svgEl.addEventListener("dblclick", () => this.resetZoom());
  }

  // Pointer position in SVG pixels.
  private localPoint(e: { clientX: number; clientY: number }) {
    const node = this.svg?.node?.() as SVGSVGElement | null;
    if (!node) return { x: 0, y: 0 };
    const r = node.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  // Which axes a gesture drives. Modifiers win; otherwise the axis gutters
  // act like the axis itself — a wheel over the score labels zooms scores,
  // a wheel over the time labels zooms time, and the plot area drives both.
  private axesFor(
    e: { shiftKey: boolean; altKey: boolean }, p: { x: number; y: number },
  ): { x: boolean; y: boolean } {
    if (e.shiftKey && !e.altKey) return { x: true, y: false };
    if (e.altKey && !e.shiftKey) return { x: false, y: true };
    if (p.x < this.plot.left) return { x: false, y: true };
    if (p.y > this.plot.top + this.plot.h) return { x: true, y: false };
    return { x: true, y: true };
  }

  private onWheel(e: WheelEvent) {
    if (this.plot.w <= 0 || this.plot.h <= 0) return;
    e.preventDefault();
    const p = this.localPoint(e);
    const axes = this.axesFor(e, p);
    // d3-zoom's delta normalization, so a notch feels the same as before.
    const unit = e.deltaMode === 1 ? 0.05 : e.deltaMode ? 1 : 0.002;
    const factor = Math.pow(2, -e.deltaY * unit);
    if (axes.x) this.zx = zoomAt(this.zx, p.x - this.plot.left, factor, this.plot.w);
    if (axes.y) this.zy = zoomAt(this.zy, p.y - this.plot.top, factor, this.plot.h);
    this.redraw();
  }

  private onPointerDown(e: PointerEvent) {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    // Stop the browser turning a pan into a text/SVG selection drag.
    e.preventDefault();
    const node = this.svg?.node?.() as SVGSVGElement | null;
    node?.setPointerCapture?.(e.pointerId);
    this.pointers.set(e.pointerId, this.localPoint(e));
    if (this.pointers.size === 1) {
      this.dragAxes = this.axesFor(e, this.localPoint(e));
    } else {
      // A second finger turns the gesture into a pinch; the next move
      // establishes the baseline spans.
      this.dragAxes = null;
      this.pinchSpan = this.currentPinchSpan();
    }
    this.applyCursor(true);
  }

  private onPointerMove(e: PointerEvent) {
    const prev = this.pointers.get(e.pointerId);
    if (!prev) {
      // Hovering: keep the cursor honest about what a drag here would do.
      this.applyCursor(false, this.axesFor(e, this.localPoint(e)));
      return;
    }
    const p = this.localPoint(e);
    this.pointers.set(e.pointerId, p);

    if (this.pointers.size >= 2) {
      this.applyPinch();
    } else if (this.dragAxes) {
      if (this.dragAxes.x) this.zx = panBy(this.zx, p.x - prev.x, this.plot.w);
      if (this.dragAxes.y) this.zy = panBy(this.zy, p.y - prev.y, this.plot.h);
    }
    this.redraw();
  }

  private onPointerUp(e: PointerEvent) {
    const node = this.svg?.node?.() as SVGSVGElement | null;
    if (node?.hasPointerCapture?.(e.pointerId)) node.releasePointerCapture(e.pointerId);
    this.pointers.delete(e.pointerId);
    if (this.pointers.size < 2) this.pinchSpan = null;
    if (this.pointers.size === 0) this.dragAxes = null;
    this.applyCursor(this.pointers.size > 0);
  }

  private currentPinchSpan(): { dx: number; dy: number } | null {
    const pts = [...this.pointers.values()];
    if (pts.length < 2) return null;
    return {
      dx: Math.abs(pts[0].x - pts[1].x),
      dy: Math.abs(pts[0].y - pts[1].y),
    };
  }

  // Two-finger pinch: x scales by how the horizontal finger span changed and
  // y by the vertical one, so spreading fingers sideways stretches time only.
  // Spans under ~24px are ignored — their ratios are noise.
  private applyPinch() {
    const span = this.currentPinchSpan();
    const prev = this.pinchSpan;
    this.pinchSpan = span;
    if (!span || !prev) return;
    const pts = [...this.pointers.values()];
    const mid = {
      x: (pts[0].x + pts[1].x) / 2 - this.plot.left,
      y: (pts[0].y + pts[1].y) / 2 - this.plot.top,
    };
    const MIN_SPAN = 24;
    if (prev.dx > MIN_SPAN && span.dx > MIN_SPAN) {
      this.zx = zoomAt(this.zx, mid.x, span.dx / prev.dx, this.plot.w);
    }
    if (prev.dy > MIN_SPAN && span.dy > MIN_SPAN) {
      this.zy = zoomAt(this.zy, mid.y, span.dy / prev.dy, this.plot.h);
    }
  }

  private applyCursor(active: boolean, axes?: { x: boolean; y: boolean }) {
    const node = this.svg?.node?.() as SVGSVGElement | null;
    if (!node) return;
    const a = axes ?? this.dragAxes ?? { x: true, y: true };
    node.style.cursor = a.x && a.y
      ? (active ? "grabbing" : "grab")
      : a.x ? "ew-resize" : "ns-resize";
  }

  // ── Scrollbars ──
  //
  // One per axis, tracking the visible window. They exist because zooming in
  // used to leave no way to tell where in the run you were looking, or to get
  // somewhere else without dragging the plot around.

  private installScrollbar(axis: "x" | "y") {
    const bar = axis === "x" ? this.scrollX : this.scrollY;
    let dragging = false;
    let last = 0;
    // Track px → plot px. They match in practice (the track is inset to the
    // plot box) but a rounded layout shouldn't make the thumb drift.
    let ratio = 1;

    const posOf = (e: PointerEvent) => {
      const r = bar.track.getBoundingClientRect();
      return axis === "x" ? e.clientX - r.left : e.clientY - r.top;
    };

    bar.track.addEventListener("pointerdown", (e: PointerEvent) => {
      const size = axis === "x" ? this.plot.w : this.plot.h;
      const r = bar.track.getBoundingClientRect();
      const trackLen = axis === "x" ? r.width : r.height;
      if (size <= 0 || trackLen <= 0) return;
      e.preventDefault();
      bar.track.setPointerCapture(e.pointerId);
      dragging = true;
      ratio = size / trackLen;
      last = posOf(e);

      // Clicking the track outside the thumb jumps the window there, centred
      // on the click; clicking the thumb itself just starts a drag.
      const z = axis === "x" ? this.zx : this.zy;
      const { offset, length } = thumbGeometry(z, size);
      const thumbStart = offset * trackLen;
      const thumbLen = length * trackLen;
      if (last < thumbStart || last > thumbStart + thumbLen) {
        const start = (last - thumbLen / 2) * ratio;
        if (axis === "x") this.zx = panToThumbStart(this.zx, start, size);
        else this.zy = panToThumbStart(this.zy, start, size);
        this.redraw();
      }
    });

    bar.track.addEventListener("pointermove", (e: PointerEvent) => {
      if (!dragging) return;
      const pos = posOf(e);
      const delta = (pos - last) * ratio;
      last = pos;
      if (axis === "x") this.zx = panByThumb(this.zx, delta, this.plot.w);
      else this.zy = panByThumb(this.zy, delta, this.plot.h);
      this.redraw();
    });

    const end = (e: PointerEvent) => {
      dragging = false;
      if (bar.track.hasPointerCapture?.(e.pointerId)) {
        bar.track.releasePointerCapture(e.pointerId);
      }
    };
    bar.track.addEventListener("pointerup", end);
    bar.track.addEventListener("pointercancel", end);
  }

  // Keep the scrollbars, the reset button and the hint in sync with the
  // current transforms. Called from computeLayout, so every redraw path and
  // every resize refreshes them.
  private syncZoomChrome() {
    if (!this.scrollX || !this.scrollY) return;
    const { left, top, w, h } = this.plot;

    // Inset each track to the plot box so the thumb lines up with the data
    // it scrolls, not with the axis labels.
    this.scrollX.track.style.marginLeft = `${left}px`;
    this.scrollX.track.style.marginRight = `${Math.max(0, this.width - left - w)}px`;
    this.scrollY.track.style.marginTop = `${top}px`;
    this.scrollY.track.style.marginBottom = `${Math.max(0, this.height - top - h)}px`;

    const gx = thumbGeometry(this.zx, w);
    this.scrollX.thumb.style.left = `${gx.offset * 100}%`;
    this.scrollX.thumb.style.width = `${gx.length * 100}%`;
    this.scrollX.track.classList.toggle("is-zoomed", isZoomed(this.zx));

    const gy = thumbGeometry(this.zy, h);
    this.scrollY.thumb.style.top = `${gy.offset * 100}%`;
    this.scrollY.thumb.style.height = `${gy.length * 100}%`;
    this.scrollY.track.classList.toggle("is-zoomed", isZoomed(this.zy));

    const zoomed = isZoomed(this.zx) || isZoomed(this.zy);
    this.zoomResetEl.style.display = zoomed ? "" : "none";
    // The hint only fits on the full-screen benchmark page, not the small
    // panel in the home grid.
    this.zoomHintEl.style.display = this.width >= 700 ? "" : "none";
  }

  // User-triggered reset (button / double-click): both axes back to fit.
  private resetZoom() {
    this.zx = identityZoom();
    this.zy = identityZoom();
    this.redraw();
  }

  // Silent reset on tab/challenge switch — the callers already redraw (or
  // clear the chart outright), so this only touches state and chrome.
  private resetZoomSilently() {
    this.zx = identityZoom();
    this.zy = identityZoom();
    this.syncZoomChrome();
  }
}

// Symlog linear→log transition point for a score domain: 1/10,000th of the
// largest magnitude (floored at 10). Everything with |score| below this sits
// in the scale's linear region; everything above spreads across log decades.
function symlogConstant(lo: number, hi: number): number {
  return Math.max(Math.abs(lo), Math.abs(hi), 1e5) / 1e4;
}

// Round a value onto a 1/2/5 × 10^k grid sized from `step` — the distance
// to the neighboring tick row — so adjacent log-mode ticks stay distinct at
// any zoom depth while still landing on round numbers.
function snapToStep(v: number, step: number): number {
  if (!Number.isFinite(step) || step <= 0) return v;
  const unit = Math.pow(10, Math.floor(Math.log10(step)));
  const m = step / unit;
  const grid = m >= 5 ? 5 * unit : m >= 2 ? 2 * unit : unit;
  return Math.round(v / grid) * grid;
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
