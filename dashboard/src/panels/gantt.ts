import * as d3 from "d3";
import { DisplayPanelBase } from "./displayPanelBase";

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

export class GanttPanel extends DisplayPanelBase<AllGanttData> {
  protected idPrefix = "gantt";

  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private axisG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private labelG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;

  private makespanEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
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
  }

  protected attachRefs(_root: HTMLElement): void {
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
  }

  protected onReset(): void {
    (this.chartG.node() as SVGGElement).innerHTML = "";
    (this.axisG.node() as SVGGElement).innerHTML = "";
    (this.labelG.node() as SVGGElement).innerHTML = "";
    this.makespanEl.textContent = "---";
  }

  protected showInstance(data: GanttData) {
    const chartNode = this.chartG.node() as SVGGElement;
    const axisNode = this.axisG.node() as SVGGElement;
    const labelNode = this.labelG.node() as SVGGElement;

    if (!data || !data.bars || !data.bars.length) {
      chartNode.innerHTML = "";
      axisNode.innerHTML = "";
      labelNode.innerHTML = "";
      this.makespanEl.textContent = "---";
      return;
    }

    const nMachines = data.num_machines;
    const makespan = data.makespan;

    const x = d3.scaleLinear().domain([0, makespan]).range([0, CHART_W]);
    const rowH = CHART_H / nMachines;
    const barH = rowH * 0.78;
    const barPad = (rowH - barH) / 2;

    const parts: string[] = [];

    for (let m = 0; m < nMachines; m++) {
      if (m % 2 === 0) {
        parts.push(`<rect x="0" y="${(m * rowH).toFixed(2)}" width="${CHART_W}" height="${rowH.toFixed(2)}" fill="rgba(255,255,255,0.015)"/>`);
      }
    }

    const ticks = x.ticks(8);
    for (const t of ticks) {
      const xv = x(t).toFixed(2);
      parts.push(`<line x1="${xv}" x2="${xv}" y1="0" y2="${CHART_H}" stroke="rgba(255,255,255,0.04)" stroke-width="0.5"/>`);
    }

    const barHStr = barH.toFixed(2);
    for (const bar of data.bars) {
      const bx = x(bar.start);
      const bw = Math.max(x(bar.end) - x(bar.start), 0.8);
      const by = bar.machine * rowH + barPad;
      parts.push(`<rect x="${bx.toFixed(2)}" y="${by.toFixed(2)}" width="${bw.toFixed(2)}" height="${barHStr}" fill="${jobColor(bar.job)}" stroke="rgba(0,0,0,0.4)" stroke-width="0.4" rx="1"/>`);
    }

    const xMakespan = x(makespan).toFixed(2);
    parts.push(`<line x1="${xMakespan}" x2="${xMakespan}" y1="0" y2="${CHART_H}" stroke="#ff5252" stroke-width="1" stroke-dasharray="4,3" opacity="0.6"/>`);
    chartNode.innerHTML = parts.join("");

    const fontSize = Math.min(11, rowH * 0.55).toFixed(2);
    const labelParts: string[] = [];
    for (let m = 0; m < nMachines; m++) {
      labelParts.push(`<text x="${MARGIN.left - 5}" y="${(m * rowH + rowH / 2).toFixed(2)}" text-anchor="end" dominant-baseline="central" fill="#3d4a5c" font-size="${fontSize}" font-family="'JetBrains Mono', monospace">${m}</text>`);
    }
    labelNode.innerHTML = labelParts.join("");

    const axisParts: string[] = [];
    for (const t of ticks) {
      const xv = x(t).toFixed(2);
      axisParts.push(`<line x1="${xv}" x2="${xv}" y1="0" y2="5" stroke="#3d4a5c" stroke-width="0.5"/>`);
      axisParts.push(`<text x="${xv}" y="16" text-anchor="middle" fill="#3d4a5c" font-size="9" font-family="'JetBrains Mono', monospace">${t}</text>`);
    }
    axisNode.innerHTML = axisParts.join("");

    this.makespanEl.textContent = makespan.toLocaleString();
  }
}
