import * as d3 from "d3";
import type { Panel, WSMessage, RouteData, AllRouteData, RoutePoint } from "../types";
import { getAgentColor, getRouteColor } from "../lib/colors";
import { formatScore } from "../lib/format";
import { getSwarmConfig } from "../lib/swarmConfig";
import { getViewedChallenge } from "../lib/viewedChallenge";

// Drawing sizes as fractions of the viewBox side length. Everything else in
// this file should reference these constants — never hardcode pixel/unit
// values, because the viewBox is fit tightly to the data and its absolute
// scale varies per dataset. Tweak these to resize elements.
const STYLE = {
  customerRadius: 0.006,          // customer dot radius
  depotSize:      0.020,          // depot diamond side length (before rotate)
  routeStroke:    0.004,          // main route line thickness
  glowStroke:     0.012,          // blurred glow halo behind each route
  routeDashOn:    0.018,          // dash length for the flowing stroke
  routeDashOff:   0.007,          // gap length for the flowing stroke
} as const;

const routeLine = d3.line<RoutePoint>()
  .x((d) => d.x)
  .y((d) => d.y)
  .curve(d3.curveCatmullRom.alpha(0.5));

function fullPath(data: RouteData, route: { path: RoutePoint[] }): RoutePoint[] {
  const depot = { x: data.depot.x, y: data.depot.y, customer_id: -1 };
  return [depot, ...route.path, depot];
}

// Sum of Euclidean distances over every leg of every vehicle's route, with the
// depot stitched onto each end. Matches how the solver computes route length.
function computeRouteDistance(data: RouteData): number {
  let total = 0;
  for (const route of data.routes) {
    const path = fullPath(data, route);
    for (let i = 0; i < path.length - 1; i++) {
      const dx = path[i + 1].x - path[i].x;
      const dy = path[i + 1].y - path[i].y;
      total += Math.sqrt(dx * dx + dy * dy);
    }
  }
  return total;
}

interface HistoryEntry {
  experiment_id: string;
  agent_name: string;
  agent_id?: string;
  score: number;
  solution_data: AllRouteData;
  created_at: string;
}

export class SolutionPanel implements Panel {
  private svg!: any;
  private routeGroup!: any;
  private customerGroup!: any;
  private depotGroup!: any;
  private scoreEl!: HTMLElement;
  private scoreDeltaEl!: HTMLElement;
  private routeDistanceEl!: HTMLElement;
  private instanceLabelEl!: HTMLElement;
  private navEl!: HTMLElement;
  private agentNameEl!: HTMLElement;
  private historyNavEl!: HTMLElement;
  private historyLabelEl!: HTMLElement;
  private historyLiveBtnEl!: HTMLElement;

  private allInstances: AllRouteData = {};
  private currentIndex = 0;
  private currentRouteData: RouteData | null = null;
  private numInstances = 1;
  // Side length of the current viewBox in SVG user units. All draw sizes
  // are computed as STYLE.* × viewSide so they stay visually consistent
  // regardless of how spread out the underlying data is.
  private viewSide = 1000;
  // Raw experiment score (sum across all instances). The displayed SCORE is
  // this divided by numInstances so it matches the leaderboard's avg metric.
  private rawScore: number | null = null;

  // All global bests seen so far, oldest to newest. Seeded from /api/replay
  // on init and appended to on live `new_global_best` events.  historyIndex
  // is the entry currently rendered on the SVG — typically the latest
  // (= "live"), but the user can step back through breakthroughs.
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
    // SolutionPanel is the VRP route renderer (depot, customer-position
    // tour drawing, BKS comparison all assume VRPTW geometry). Each other
    // challenge has its own dedicated panel wired in main.ts; this `if`
    // guard remains as a safety net for any future challenge added before
    // its panel exists.
    const activeChallenge = getSwarmConfig().active_challenge;
    if (activeChallenge !== "vehicle_routing") {
      container.innerHTML = `
        <div class="panel-inner solution-panel">
          <div class="panel-label">VISUALIZATION</div>
          <div class="solution-agent-name">Active challenge: ${activeChallenge}</div>
          <div style="padding: 24px; opacity: 0.6; text-align: center; line-height: 1.6;">
            Per-challenge visualization not yet wired up.<br>
            Score, leaderboard, feed, and chart panels still work.
          </div>
        </div>`;
      return;
    }
    container.innerHTML = `
      <div class="panel-inner solution-panel">
        <div class="panel-label">ROUTES</div>
        <div class="solution-agent-name" id="solution-agent-name"></div>
        <div class="solution-history-nav" id="solution-history-nav" style="display:none">
          <button class="solution-nav-btn" id="solution-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="solution-history-label" id="solution-history-label"></span>
          <button class="solution-nav-btn" id="solution-hist-next" title="Next global best">&rsaquo;</button>
          <button class="solution-history-live" id="solution-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="solution-nav" id="solution-nav" style="display:none">
          <button class="solution-nav-btn" id="solution-prev">&lsaquo;</button>
          <span class="solution-instance-label" id="solution-instance-label"></span>
          <button class="solution-nav-btn" id="solution-next">&rsaquo;</button>
        </div>
        <div class="solution-svg-wrap" id="solution-svg-wrap">
          <svg id="solution-svg"></svg>
        </div>
        <div class="solution-route-distance">
          <div class="solution-sub-label">ROUTE DISTANCE</div>
          <div class="solution-sub-value" id="solution-route-distance">---</div>
        </div>
        <div class="solution-score">
          <div class="solution-score-label">SCORE</div>
          <div class="solution-score-value" id="solution-score">---</div>
          <div class="solution-score-delta" id="solution-score-delta"></div>
        </div>
      </div>
    `;

    this.scoreEl = document.getElementById("solution-score")!;
    this.scoreDeltaEl = document.getElementById("solution-score-delta")!;
    this.routeDistanceEl = document.getElementById("solution-route-distance")!;
    this.instanceLabelEl = document.getElementById("solution-instance-label")!;
    this.navEl = document.getElementById("solution-nav")!;
    this.agentNameEl = document.getElementById("solution-agent-name")!;
    this.historyNavEl = document.getElementById("solution-history-nav")!;
    this.historyLabelEl = document.getElementById("solution-history-label")!;
    this.historyLiveBtnEl = document.getElementById("solution-hist-live")!;

    document.getElementById("solution-prev")!.addEventListener("click", () => this.navigate(-1));
    document.getElementById("solution-next")!.addEventListener("click", () => this.navigate(1));
    document.getElementById("solution-hist-prev")!.addEventListener("click", () => this.navigateHistory(-1));
    document.getElementById("solution-hist-next")!.addEventListener("click", () => this.navigateHistory(1));
    this.historyLiveBtnEl.addEventListener("click", () => {
      if (!this.historyEntries.length) return;
      this.historyIndex = this.historyEntries.length - 1;
      this.applyHistoryEntry();
    });

    this.svg = d3.select("#solution-svg");
    this.svg
      .attr("viewBox", "0 0 1000 1000")
      .attr("preserveAspectRatio", "xMidYMid meet");

    const defs = this.svg.append("defs");
    const filter = defs.append("filter").attr("id", "route-glow");
    filter.append("feGaussianBlur").attr("stdDeviation", "1.5").attr("result", "blur");
    const merge = filter.append("feMerge");
    merge.append("feMergeNode").attr("in", "blur");
    merge.append("feMergeNode").attr("in", "SourceGraphic");

    this.routeGroup = this.svg.append("g").attr("class", "routes");
    this.customerGroup = this.svg.append("g").attr("class", "customers");
    this.depotGroup = this.svg.append("g").attr("class", "depot");

    // Make the SVG element a square sized to the largest square that fits
    // inside the wrap. Without this the SVG fills the wrap rectangle but the
    // 1:1 viewBox letterboxes a square inside it, leaving large empty side
    // margins on a wide panel.
    const wrap = document.getElementById("solution-svg-wrap")!;
    const resize = () => {
      const size = Math.max(0, Math.min(wrap.clientWidth, wrap.clientHeight));
      this.svg.attr("width", size).attr("height", size);
    };
    new ResizeObserver(resize).observe(wrap);
    resize();

    setInterval(() => {
      if (this.instanceKeys.length > 1) {
        this.navigate(1);
      }
    }, 8000);

    // Resolve API base URL (same pattern other panels use).
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
    this.fetchHistory();
  }

  // Seed historyEntries from /api/replay. Safe to race with WS hydration:
  // entries are deduped by experiment_id and merged in chronological order.
  private async fetchHistory() {
    try {
      // Filter by the user's currently-viewed challenge so VRP shows VRP
      // history regardless of the swarm's active_challenge. Without the
      // filter, the server resolves to active_challenge and returns rows
      // for the wrong challenge — which for VRP looks like "lost history".
      const ch = encodeURIComponent(getViewedChallenge());
      const res = await fetch(`${this.apiUrl}/api/replay?challenge=${ch}`);
      if (!res.ok) return;
      const rows: any[] = await res.json();
      const fetched: HistoryEntry[] = rows
        .filter((r) => r && r.solution_data)
        .map((r) => ({
          experiment_id: r.experiment_id,
          agent_name: r.agent_name,
          agent_id: r.agent_id,
          score: r.score,
          solution_data: r.solution_data,
          created_at: r.created_at,
        }));
      const existingIds = new Set(this.historyEntries.map((e) => e.experiment_id));
      const merged = [
        ...fetched.filter((e) => !existingIds.has(e.experiment_id)),
        ...this.historyEntries,
      ];
      merged.sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
      this.historyEntries = merged;
      const wasAtLatest = this.isAtLatest();
      if (wasAtLatest && this.historyEntries.length) {
        this.historyIndex = this.historyEntries.length - 1;
        this.applyHistoryEntry();
      }
      this.updateHistoryLabel();
    } catch {
      // network/transport errors are non-fatal — panel works without history
    }
  }

  private navigateHistory(delta: number) {
    if (!this.historyEntries.length) return;
    const next = Math.max(
      0,
      Math.min(this.historyEntries.length - 1, this.historyIndex + delta),
    );
    if (next === this.historyIndex) return;
    this.historyIndex = next;
    this.applyHistoryEntry();
  }

  // Render the currently-selected HistoryEntry: swap solution_data, redraw,
  // update score/agent/delta. Safe to call whenever historyIndex changes.
  private applyHistoryEntry() {
    const entry = this.historyEntries[this.historyIndex];
    if (!entry) return;

    this.rawScore = entry.score;
    this.allInstances = entry.solution_data;
    this.updateViewBox();

    this.agentNameEl.textContent = entry.agent_name;
    this.agentNameEl.style.color = entry.agent_id
      ? getAgentColor(entry.agent_id)
      : "";

    const keys = this.instanceKeys;
    if (this.currentIndex >= keys.length) this.currentIndex = 0;
    this.updateInstanceLabel();
    if (keys.length > 0) {
      this.showInstance(this.allInstances[keys[this.currentIndex]]);
    }

    this.scoreEl.textContent = formatScore(entry.score);

    // Score delta = improvement this entry represented over the previous
    // historical best. Shown as a negative score change ("-X.XXXXX%") in
    // green, matching the live-message format.
    if (this.historyIndex > 0) {
      const prev = this.historyEntries[this.historyIndex - 1];
      const pct = prev.score > 0 ? ((prev.score - entry.score) / prev.score) * 100 : 0;
      const scoreChange = -pct;
      const sign = scoreChange >= 0 ? "+" : "";
      this.scoreDeltaEl.textContent = `${sign}${scoreChange.toFixed(5)}% vs prev best`;
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

  // Compute a square viewBox that tightly bounds *all* instances' data with a
  // small padding margin. Using all instances (rather than per-instance) keeps
  // the zoom stable as you click through them.
  private updateViewBox() {
    const all = Object.values(this.allInstances);
    if (all.length === 0) {
      this.viewSide = 1000;
      this.svg.attr("viewBox", "0 0 1000 1000");
      return;
    }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const inst of all) {
      const consider = (x: number, y: number) => {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      };
      consider(inst.depot.x, inst.depot.y);
      for (const route of inst.routes) {
        for (const p of route.path) consider(p.x, p.y);
      }
    }
    if (!isFinite(minX)) {
      this.viewSide = 1000;
      this.svg.attr("viewBox", "0 0 1000 1000");
      return;
    }
    const w = maxX - minX;
    const h = maxY - minY;
    const side = Math.max(w, h, 1);
    const padding = side * 0.06;
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const finalSide = side + padding * 2;
    const x = cx - finalSide / 2;
    const y = cy - finalSide / 2;
    this.viewSide = finalSide;
    this.svg.attr("viewBox", `${x} ${y} ${finalSide} ${finalSide}`);
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
    const key = keys[this.currentIndex];
    const label = key.replace(/\.txt$/, "");
    this.instanceLabelEl.textContent = `${label}  (${this.currentIndex + 1}/${keys.length})`;
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.allInstances = {};
      this.currentRouteData = null;
      this.currentIndex = 0;
      this.rawScore = null;
      this.viewSide = 1000;
      this.historyEntries = [];
      this.historyIndex = -1;
      this.routeGroup.selectAll("*").remove();
      this.customerGroup.selectAll("*").remove();
      this.depotGroup.selectAll("*").remove();
      this.svg.attr("viewBox", "0 0 1000 1000");
      this.scoreEl.textContent = "---";
      this.scoreDeltaEl.textContent = "";
      this.routeDistanceEl.textContent = "---";
      this.navEl.style.display = "none";
      this.historyNavEl.style.display = "none";
      this.instanceLabelEl.textContent = "";
      this.agentNameEl.textContent = "";
      this.agentNameEl.style.color = "";
      return;
    }

    if (msg.type === "stats_update") {
      if (msg.num_instances) this.numInstances = msg.num_instances;
      // Show score even before any route data has arrived. Once route data
      // exists, new_global_best is the source of truth. Score is already a
      // per-instance average from the server.
      if (msg.best_score != null && !this.currentRouteData) {
        this.rawScore = msg.best_score;
        this.scoreEl.textContent = formatScore(msg.best_score);
      }
    }

    if (msg.type === "new_global_best" && msg.solution_data) {
      if (msg.num_instances) this.numInstances = msg.num_instances;

      const entry: HistoryEntry = {
        experiment_id: msg.experiment_id,
        agent_name: msg.agent_name,
        agent_id: msg.agent_id,
        score: msg.score,
        solution_data: msg.solution_data,
        created_at: msg.timestamp,
      };

      // Dedupe by experiment_id. The same entry can arrive via several paths:
      // (a) initial WS hydration, (b) /api/replay fetch, (c) the R-key replay
      // re-dispatching historical bests. Case (c) is special — the user is
      // deliberately iterating through history, so if we recognize the entry
      // we jump to that specific historical index rather than snapping to
      // latest; that preserves the replay animation.
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
          // User is browsing an older best — don't yank them away. Just
          // refresh the counter so they know a new entry landed.
          this.updateHistoryLabel();
        }
      }
    }
  }

  // Immediate, non-animated draw of one instance's route data.
  private showInstance(data: RouteData) {
    this.currentRouteData = data;

    this.routeGroup.selectAll("*").remove();
    this.customerGroup.selectAll("*").remove();
    this.depotGroup.selectAll("*").remove();

    const s = this.viewSide;
    const customerR = STYLE.customerRadius * s;
    const routeW = STYLE.routeStroke * s;
    const glowW = STYLE.glowStroke * s;
    const dashOn = STYLE.routeDashOn * s;
    const dashOff = STYLE.routeDashOff * s;

    data.routes.forEach((route, i) => {
      const path = fullPath(data, route);
      const color = getRouteColor(i);

      // Glow halo
      this.routeGroup.append("path")
        .datum(path)
        .attr("d", routeLine as any)
        .attr("fill", "none")
        .attr("stroke", color)
        .attr("stroke-width", glowW)
        .attr("stroke-opacity", 0.1)
        .attr("filter", "url(#route-glow)");

      // Main path
      this.routeGroup.append("path")
        .datum(path)
        .attr("d", routeLine as any)
        .attr("fill", "none")
        .attr("stroke", color)
        .attr("stroke-width", routeW)
        .attr("stroke-opacity", 0.9)
        .attr("stroke-dasharray", `${dashOn} ${dashOff}`)
        .attr("class", "route-flowing");

      // Customers
      route.path.forEach((pt) => {
        this.customerGroup.append("circle")
          .attr("cx", pt.x)
          .attr("cy", pt.y)
          .attr("r", customerR)
          .attr("fill", color)
          .attr("opacity", 0.75);
      });
    });

    // Depot
    const depotSize = STYLE.depotSize * s;
    this.depotGroup.append("rect")
      .attr("x", data.depot.x - depotSize / 2)
      .attr("y", data.depot.y - depotSize / 2)
      .attr("width", depotSize)
      .attr("height", depotSize)
      .attr("fill", "#fff")
      .attr("opacity", 0.9)
      .attr("transform", `rotate(45, ${data.depot.x}, ${data.depot.y})`)
      .attr("class", "depot-pulse");

    // ROUTE DISTANCE = total Euclidean distance for the currently shown instance
    this.routeDistanceEl.textContent = computeRouteDistance(data).toFixed(1);
  }
}
