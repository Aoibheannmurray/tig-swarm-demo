import { axisBottom, axisLeft, type AxisScale } from "d3-axis";
import { extent, max, min } from "d3-array";
import { format } from "d3-format";
import { scaleLinear } from "d3-scale";
import { select } from "d3-selection";
import { curveStepAfter, line } from "d3-shape";

interface ScorePoint {
  score: number;
  created_at: string;
}

interface Trajectory {
  id: string;
  started_at: string;
  status: "active" | "inactive";
  current_score: number | null;
  num_edits: number;
  num_improvements: number;
  momentum: number;
  // Distinct agents that have ever published an experiment on this
  // trajectory. The server computes this with COUNT(DISTINCT agent_id).
  num_agents: number;
  // Number of times this trajectory has gone into the inactive pool. A
  // trajectory can be re-deactivated after being adopted from the pool, so
  // this can be > 1.
  num_deactivations: number;
  edits_since_improvement: number;
  deactivated_at: string | null;
  score_history: ScorePoint[];
}

interface TrajectoriesResponse {
  total: number;
  active: number;
  inactive: number;
  trajectories: Trajectory[];
}

import { PALETTE, token } from "../../lib/colors";
import { getViewedChallenge } from "../../lib/viewedChallenge";

function trajColor(index: number): string {
  return PALETTE[index % PALETTE.length];
}

const AXIS_TEXT = () => token("--ink-dim", "rgba(26,26,26,0.50)");
const AXIS_LINE = () => token("--border-default", "rgba(26,26,26,0.15)");
const GRID_LINE = () => token("--border-subtle", "rgba(26,26,26,0.08)");

function fmtScore(v: number | null): string {
  if (v == null) return "---";
  return v.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${mo}-${dd} ${hh}:${mm}`;
}

function fmtMomentum(m: number): string {
  return m.toFixed(2);
}

// Chart "view" the user can cycle through:
//  - { kind: "all" }     → every trajectory overlaid on absolute time
//  - { kind: "single" }  → one trajectory, x-axis re-zeroed at its start
type ChartView = { kind: "all" } | { kind: "single"; trajectoryId: string };

export class TrajectoriesPanel {
  private container!: HTMLElement;
  private apiUrl = "";
  private data: TrajectoriesResponse | null = null;
  private refreshTimer: ReturnType<typeof setInterval> | null = null;
  private view: ChartView = { kind: "all" };

  init(container: HTMLElement, apiUrl: string) {
    // init() is called again on every challenge switch from
    // pages/trajectories/main.ts; clear the prior 15s poll so it doesn't
    // stack.
    if (this.refreshTimer !== null) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
    this.container = container;
    this.apiUrl = apiUrl;

    container.innerHTML = `
      <div class="panel-inner traj-page">
        <div class="traj-header">
          <div class="traj-title-row">
            <div class="traj-title">
              <img class="stats-mark" src="/prometheus-icon.png" alt="" draggable="false" />
              <span class="traj-title-text">Trajectory Profile</span>
            </div>
            <div class="traj-nav">
              <a class="ideas-nav-link" href="/">Dashboard</a>
              <a class="ideas-nav-link" href="/ideas.html">Ideas</a>
              <a class="ideas-nav-link" href="/diversity.html">Diversity</a>
              <a class="ideas-nav-link" href="/benchmark.html">Benchmark</a>
              <span class="ideas-nav-active">Trajectories</span>
            </div>
          </div>
          <div class="traj-counters" id="traj-counters">
            <div class="stat-chip">
              <span class="stat-label">Total</span>
              <span class="stat-value" id="traj-total">0</span>
            </div>
            <div class="stat-chip">
              <span class="stat-label">Active</span>
              <span class="stat-value" id="traj-active" style="color:var(--success)">0</span>
            </div>
            <div class="stat-chip">
              <span class="stat-label">Inactive</span>
              <span class="stat-value" id="traj-inactive" style="color:var(--ink-dim)">0</span>
            </div>
          </div>
        </div>
        <div class="traj-body">
          <div class="traj-chart-area">
            <div class="traj-chart-header">
              <div class="panel-label">SCORE PROGRESSION</div>
              <div class="traj-chart-tabs" id="traj-chart-tabs">
                <button type="button" class="traj-chart-tab-btn" id="traj-chart-prev">&lsaquo;</button>
                <span class="traj-chart-tab-label" id="traj-chart-label">ALL TRAJECTORIES</span>
                <button type="button" class="traj-chart-tab-btn" id="traj-chart-next">&rsaquo;</button>
              </div>
            </div>
            <div class="traj-chart-wrap" id="traj-chart-wrap">
              <svg id="traj-chart-svg"></svg>
            </div>
          </div>
          <div class="traj-table-area">
            <div class="panel-label">ALL TRAJECTORIES</div>
            <div class="traj-table-header">
              <span class="traj-col traj-col-id">#</span>
              <span class="traj-col traj-col-status">STATUS</span>
              <span class="traj-col traj-col-started">STARTED</span>
              <span class="traj-col traj-col-score">SCORE</span>
              <span class="traj-col traj-col-edits">EDITS</span>
              <span class="traj-col traj-col-improvements">IMPR</span>
              <span class="traj-col traj-col-momentum">MOMENTUM</span>
              <span class="traj-col traj-col-stagnation">STAG</span>
              <span class="traj-col traj-col-agents">AGENTS</span>
              <span class="traj-col traj-col-deactivations">DEACT</span>
            </div>
            <div class="traj-table-list" id="traj-table-list"></div>
          </div>
        </div>
      </div>
    `;

    document.getElementById("traj-chart-prev")!.addEventListener(
      "click", () => this.cycleView(-1),
    );
    document.getElementById("traj-chart-next")!.addEventListener(
      "click", () => this.cycleView(1),
    );

    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 15000);
  }

  private async fetchData() {
    try {
      // Pin to the viewed challenge — otherwise the server defaults to its
      // active challenge and the page always shows that one's trajectories,
      // regardless of what the user picked in the selector.
      const challenge = getViewedChallenge();
      const res = await fetch(
        `${this.apiUrl}/api/trajectories?challenge=${encodeURIComponent(challenge)}`,
      );
      if (!res.ok) return;
      this.data = await res.json();
      this.render();
    } catch {
      // non-fatal
    }
  }

  private render() {
    if (!this.data) return;
    const { total, active, inactive, trajectories } = this.data;

    document.getElementById("traj-total")!.textContent = String(total);
    document.getElementById("traj-active")!.textContent = String(active);
    document.getElementById("traj-inactive")!.textContent = String(inactive);

    // If the trajectory we're viewing has been removed from the response,
    // fall back to the all-trajectories overlay so the chart doesn't go
    // blank with no way to recover.
    if (this.view.kind === "single") {
      const targetId = this.view.trajectoryId;
      if (!trajectories.some(t => t.id === targetId)) {
        this.view = { kind: "all" };
      }
    }

    this.renderChart(trajectories);
    this.renderTable(trajectories);
    this.renderViewLabel(trajectories);
  }

  private cycleView(delta: number) {
    if (!this.data) return;
    // The cycle order is [all, traj#0, traj#1, …]. Trajectories are listed
    // in the same order returned by the server (started_at DESC), so the
    // "next" arrow walks through them newest-first.
    const trajectories = this.data.trajectories;
    const ids = trajectories.map(t => t.id);
    const order: (string | null)[] = [null, ...ids];

    let idx: number;
    if (this.view.kind === "all") idx = 0;
    else idx = Math.max(0, ids.indexOf(this.view.trajectoryId) + 1);

    const next = (idx + delta + order.length) % order.length;
    const target = order[next];
    this.view = target == null ? { kind: "all" } : { kind: "single", trajectoryId: target };

    this.renderChart(trajectories);
    this.renderViewLabel(trajectories);
  }

  private renderViewLabel(trajectories: Trajectory[]) {
    const el = document.getElementById("traj-chart-label");
    if (!el) return;
    const view = this.view;
    if (view.kind === "all") {
      el.textContent = "ALL TRAJECTORIES";
      el.style.color = "";
      return;
    }
    const idx = trajectories.findIndex(t => t.id === view.trajectoryId);
    const t = trajectories[idx];
    if (!t) {
      el.textContent = "ALL TRAJECTORIES";
      return;
    }
    el.textContent = `TRAJECTORY ${t.id.slice(0, 6)}`;
    el.style.color = trajColor(idx);
  }

  private renderChart(trajectories: Trajectory[]) {
    const wrap = document.getElementById("traj-chart-wrap")!;
    const svg = select("#traj-chart-svg");
    svg.selectAll("*").remove();

    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (w <= 0 || h <= 0) return;

    svg.attr("width", w).attr("height", h);

    const margin = { top: 20, right: 20, bottom: 30, left: 70 };
    const cw = w - margin.left - margin.right;
    const ch = h - margin.top - margin.bottom;

    const view = this.view;
    let trajectoriesToDraw = trajectories;
    if (view.kind === "single") {
      trajectoriesToDraw = trajectories.filter(t => t.id === view.trajectoryId);
    }
    const withHistory = trajectoriesToDraw.filter((t) => t.score_history.length > 0);
    if (withHistory.length === 0) {
      svg.append("text")
        .attr("x", w / 2).attr("y", h / 2)
        .attr("text-anchor", "middle")
        .attr("fill", AXIS_TEXT())
        .attr("font-size", 13)
        .attr("font-family", "var(--ui)")
        .text(view.kind === "single" ? "No data on this trajectory yet" : "No trajectory data yet");
      return;
    }

    // X scale: in the all-trajectories view we plot wall-clock time. In the
    // per-trajectory view we re-zero the axis at the trajectory's started_at
    // so the user can see the relative time spent on this attempt.
    const isSingle = this.view.kind === "single";
    let xDomain: [number, number];
    if (isSingle) {
      const t = withHistory[0];
      const start = new Date(t.started_at).getTime();
      const end = t.score_history.length
        ? Math.max(...t.score_history.map(p => new Date(p.created_at).getTime()))
        : start;
      // Pad the right edge so the last point is visible; widen the domain
      // slightly when start == end (single point) so the line still draws.
      const span = Math.max(end - start, 1);
      xDomain = [0, span * 1.05];
    } else {
      const allTimes: Date[] = [];
      for (const t of withHistory) {
        for (const p of t.score_history) allTimes.push(new Date(p.created_at));
      }
      const ext = extent(allTimes) as [Date, Date];
      const startMs = ext[0].getTime();
      const endMs = ext[1].getTime();
      // Pad the right edge so the active-trajectory extension below is
      // visible. Without this, xDomain[1] equals the last data point's
      // timestamp, so the step-after hold has zero width and the latest
      // score renders as only an endpoint dot — making the ALL view look
      // stale vs the per-trajectory view (which already pads by 5%).
      const span = Math.max(endMs - startMs, 1);
      xDomain = [startMs, endMs + span * 0.05];
    }

    const allScores: number[] = [];
    for (const t of withHistory) {
      for (const p of t.score_history) allScores.push(p.score);
    }
    const yMin = min(allScores)!;
    const yMax = max(allScores)!;
    const yPad = Math.max(Math.abs(yMax - yMin) * 0.08, 1);

    const x = scaleLinear().domain(xDomain).range([0, cw]);
    const y = scaleLinear().domain([yMin - yPad, yMax + yPad]).range([ch, 0]);

    const g = svg.append("g")
      .attr("transform", `translate(${margin.left},${margin.top})`);

    // Grid
    const yTicks = y.ticks(6);
    for (const t of yTicks) {
      g.append("line")
        .attr("x1", 0).attr("x2", cw)
        .attr("y1", y(t)).attr("y2", y(t))
        .attr("stroke", GRID_LINE())
        .attr("stroke-width", 0.5);
    }

    // X axis. In the single-trajectory view ticks are formatted as
    // "+m:ss" elapsed since the trajectory started; in the all-overlay
    // view they're wall-clock HH:MM.
    const xAxis = g.append("g").attr("transform", `translate(0,${ch})`);
    if (isSingle) {
      const xTicks = x.ticks(6);
      xAxis.selectAll("line.tick").data(xTicks).enter().append("line")
        .attr("class", "tick")
        .attr("x1", d => x(d as number))
        .attr("x2", d => x(d as number))
        .attr("y1", 0).attr("y2", 4)
        .attr("stroke", AXIS_LINE());
      xAxis.selectAll("text.tick").data(xTicks).enter().append("text")
        .attr("class", "tick")
        .attr("x", d => x(d as number))
        .attr("y", 16)
        .attr("text-anchor", "middle")
        .attr("fill", AXIS_TEXT())
        .attr("font-size", 9)
        .text(d => formatRelative(d as number));
      xAxis.append("line")
        .attr("x1", 0).attr("x2", cw)
        .attr("y1", 0).attr("y2", 0)
        .attr("stroke", AXIS_LINE());
    } else {
      xAxis.call(
        axisBottom(x as unknown as AxisScale<number>)
          .ticks(6)
          .tickFormat((d) => {
            const dt = new Date(d as number);
            return `${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}`;
          }) as any,
      );
      xAxis.selectAll("text").attr("fill", AXIS_TEXT()).attr("font-size", 9);
      xAxis.selectAll("line").attr("stroke", AXIS_LINE());
      xAxis.select(".domain").attr("stroke", AXIS_LINE());
    }

    // Y axis
    const yAxis = g.append("g");
    yAxis.call(axisLeft(y).ticks(6).tickFormat(format(",.0f")) as any);
    yAxis.selectAll("text").attr("fill", AXIS_TEXT()).attr("font-size", 9);
    yAxis.selectAll("line").attr("stroke", AXIS_LINE());
    yAxis.select(".domain").attr("stroke", AXIS_LINE());

    // Step function lines per trajectory
    const stepLine = line<{ t: number; s: number }>()
      .x((d) => x(d.t))
      .y((d) => y(d.s))
      .curve(curveStepAfter);

    for (let i = 0; i < withHistory.length; i++) {
      const traj = withHistory[i];
      // Preserve the original color when in single view by looking up the
      // trajectory's index in the FULL list, not the filtered list.
      const fullIndex = isSingle ? trajectories.findIndex(t => t.id === traj.id) : i;
      const color = trajColor(fullIndex >= 0 ? fullIndex : i);
      const isInactive = traj.status === "inactive";
      const start = isSingle ? new Date(traj.started_at).getTime() : 0;
      const pts = traj.score_history.map((p) => ({
        t: isSingle ? new Date(p.created_at).getTime() - start : new Date(p.created_at).getTime(),
        s: p.score,
      }));

      // Extend the last point to the right edge for active trajectories
      if (!isInactive && pts.length > 0) {
        pts.push({ t: xDomain[1], s: pts[pts.length - 1].s });
      }

      g.append("path")
        .datum(pts)
        .attr("d", stepLine as any)
        .attr("fill", "none")
        .attr("stroke", color)
        .attr("stroke-width", isInactive ? 1 : 2)
        .attr("stroke-opacity", isInactive ? 0.35 : 0.85)
        .attr("stroke-dasharray", isInactive ? "4,3" : "none");

      // Endpoint dot
      if (pts.length > 0) {
        const last = pts[pts.length - 1];
        g.append("circle")
          .attr("cx", x(last.t)).attr("cy", y(last.s))
          .attr("r", isInactive ? 2.5 : 3.5)
          .attr("fill", color)
          .attr("opacity", isInactive ? 0.4 : 0.9);
      }
    }
  }

  private renderTable(trajectories: Trajectory[]) {
    const list = document.getElementById("traj-table-list")!;

    if (trajectories.length === 0) {
      list.innerHTML = `<div class="traj-empty">No trajectories yet</div>`;
      return;
    }

    // Sort: active first, then by current_score descending (higher is better for quality)
    const sorted = [...trajectories].sort((a, b) => {
      if (a.status !== b.status) return a.status === "active" ? -1 : 1;
      const sa = a.current_score ?? -Infinity;
      const sb = b.current_score ?? -Infinity;
      return sb - sa;
    });

    list.innerHTML = sorted.map((t) => {
      const isInactive = t.status === "inactive";
      const color = trajColor(trajectories.indexOf(t));
      const cls = isInactive ? "traj-row traj-row--inactive" : "traj-row";
      return `
        <div class="${cls}" data-traj-id="${t.id}">
          <span class="traj-col traj-col-id">
            <span class="traj-dot" style="background:${color}"></span>
            ${t.id.slice(0, 6)}
          </span>
          <span class="traj-col traj-col-status">
            <span class="traj-status-badge traj-status-${t.status}">${t.status}</span>
          </span>
          <span class="traj-col traj-col-started">${fmtDate(t.started_at)}</span>
          <span class="traj-col traj-col-score">${fmtScore(t.current_score)}</span>
          <span class="traj-col traj-col-edits">${t.num_edits}</span>
          <span class="traj-col traj-col-improvements">${t.num_improvements}</span>
          <span class="traj-col traj-col-momentum">${fmtMomentum(t.momentum)}</span>
          <span class="traj-col traj-col-stagnation">${t.edits_since_improvement}</span>
          <span class="traj-col traj-col-agents">${t.num_agents}</span>
          <span class="traj-col traj-col-deactivations">${t.num_deactivations}</span>
        </div>
      `;
    }).join("");

    // Click a row to focus the chart on that trajectory.
    list.querySelectorAll<HTMLElement>(".traj-row").forEach((row) => {
      row.addEventListener("click", () => {
        const id = row.dataset.trajId || "";
        if (!id) return;
        this.view = { kind: "single", trajectoryId: id };
        if (this.data) {
          this.renderChart(this.data.trajectories);
          this.renderViewLabel(this.data.trajectories);
        }
      });
    });
  }
}

function formatRelative(ms: number): string {
  const totalSec = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m === 0) return `+${s}s`;
  return `+${m}:${s.toString().padStart(2, "0")}`;
}
