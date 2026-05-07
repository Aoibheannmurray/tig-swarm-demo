import * as d3 from "d3";

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
  num_agents: number;
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

import { PALETTE, token } from "../lib/colors";

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

export class TrajectoriesPanel {
  private container!: HTMLElement;
  private apiUrl = "";
  private data: TrajectoriesResponse | null = null;
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  init(container: HTMLElement, apiUrl: string) {
    this.container = container;
    this.apiUrl = apiUrl;

    container.innerHTML = `
      <div class="panel-inner traj-page">
        <div class="traj-header">
          <div class="traj-title-row">
            <div class="traj-title">
              <i class="ph ph-flame stats-mark" aria-hidden="true"></i>
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
            <div class="panel-label">SCORE PROGRESSION</div>
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
            </div>
            <div class="traj-table-list" id="traj-table-list"></div>
          </div>
        </div>
      </div>
    `;

    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 15000);
  }

  private async fetchData() {
    try {
      const res = await fetch(`${this.apiUrl}/api/trajectories`);
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

    this.renderChart(trajectories);
    this.renderTable(trajectories);
  }

  private renderChart(trajectories: Trajectory[]) {
    const wrap = document.getElementById("traj-chart-wrap")!;
    const svg = d3.select("#traj-chart-svg");
    svg.selectAll("*").remove();

    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (w <= 0 || h <= 0) return;

    svg.attr("width", w).attr("height", h);

    const margin = { top: 20, right: 20, bottom: 30, left: 70 };
    const cw = w - margin.left - margin.right;
    const ch = h - margin.top - margin.bottom;

    const withHistory = trajectories.filter((t) => t.score_history.length > 0);
    if (withHistory.length === 0) {
      svg.append("text")
        .attr("x", w / 2).attr("y", h / 2)
        .attr("text-anchor", "middle")
        .attr("fill", AXIS_TEXT())
        .attr("font-size", 13)
        .attr("font-family", "var(--ui)")
        .text("No trajectory data yet");
      return;
    }

    let allTimes: Date[] = [];
    let allScores: number[] = [];
    for (const t of withHistory) {
      for (const p of t.score_history) {
        allTimes.push(new Date(p.created_at));
        allScores.push(p.score);
      }
    }

    const xDomain = d3.extent(allTimes) as [Date, Date];
    const yMin = d3.min(allScores)!;
    const yMax = d3.max(allScores)!;
    const yPad = Math.max(Math.abs(yMax - yMin) * 0.08, 1);

    const x = d3.scaleTime().domain(xDomain).range([0, cw]);
    const y = d3.scaleLinear().domain([yMin - yPad, yMax + yPad]).range([ch, 0]);

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

    // X axis
    const xAxis = g.append("g").attr("transform", `translate(0,${ch})`);
    xAxis.call(
      d3.axisBottom(x).ticks(6).tickFormat((d) => {
        const dt = d as Date;
        return `${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}`;
      }) as any
    );
    xAxis.selectAll("text").attr("fill", AXIS_TEXT()).attr("font-size", 9);
    xAxis.selectAll("line").attr("stroke", AXIS_LINE());
    xAxis.select(".domain").attr("stroke", AXIS_LINE());

    // Y axis
    const yAxis = g.append("g");
    yAxis.call(d3.axisLeft(y).ticks(6).tickFormat(d3.format(",.0f")) as any);
    yAxis.selectAll("text").attr("fill", AXIS_TEXT()).attr("font-size", 9);
    yAxis.selectAll("line").attr("stroke", AXIS_LINE());
    yAxis.select(".domain").attr("stroke", AXIS_LINE());

    // Step function lines per trajectory
    const stepLine = d3.line<{ t: Date; s: number }>()
      .x((d) => x(d.t))
      .y((d) => y(d.s))
      .curve(d3.curveStepAfter);

    for (let i = 0; i < withHistory.length; i++) {
      const traj = withHistory[i];
      const color = trajColor(i);
      const isInactive = traj.status === "inactive";
      const pts = traj.score_history.map((p) => ({
        t: new Date(p.created_at),
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

    list.innerHTML = sorted.map((t, i) => {
      const isInactive = t.status === "inactive";
      const color = trajColor(trajectories.indexOf(t));
      const cls = isInactive ? "traj-row traj-row--inactive" : "traj-row";
      return `
        <div class="${cls}">
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
        </div>
      `;
    }).join("");
  }
}
