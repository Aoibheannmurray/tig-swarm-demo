import { scaleLinear } from "d3-scale";
import { select, type Selection } from "d3-selection";
import { DisplayPanelBase } from "./base";
import { token } from "../lib/colors";

const AXIS_TEXT = () => token("--ink-dim", "rgba(26,26,26,0.50)");
const ROW_STRIPE = () => "rgba(26, 26, 26, 0.025)";
const TICK_LINE = () => "rgba(26, 26, 26, 0.06)";
const DANGER = () => token("--danger", "#8B2D2D");

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

// Fixed palette for job bars. Jobs 0–7 use these literal swatches. Jobs
// beyond the palette fall through to a procedural generator below, which
// holds saturation/lightness in the same muted-earth-tone range so
// generated colors blend with the base palette.
const JOB_PALETTE_BASE = [
  "#B8541F",
  "#A66E45",
  "#C68F3E",
  "#6B7F4E",
  "#4A8C8A",
  "#4E6B85",
  "#8B6B8C",
  "#7A4F6E",
];

function jobColor(job: number): string {
  const i = ((job % 1e6) + 1e6) % 1e6;
  if (i < JOB_PALETTE_BASE.length) return JOB_PALETTE_BASE[i];
  // Golden-angle hue walk starting at +100° so the first generated color
  // lands in the green/cyan band (gap in the base palette). Lightness
  // alternates between two bands (38% / 56%) so adjacent generated jobs
  // get clear value contrast — at S=28% the eye can't separate close
  // hues alone. (Temporary — revisit if the band feels too stripey.)
  const k = i - JOB_PALETTE_BASE.length;
  const hue = (k * 137.508 + 100) % 360;
  const lightness = k % 2 === 0 ? 38 : 56;
  return `hsl(${hue.toFixed(1)}, 28%, ${lightness}%)`;
}

export class GanttPanel extends DisplayPanelBase<AllGanttData> {
  protected idPrefix = "gantt";

  private svg!: Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: Selection<SVGGElement, unknown, HTMLElement, any>;
  private axisG!: Selection<SVGGElement, unknown, HTMLElement, any>;
  private labelG!: Selection<SVGGElement, unknown, HTMLElement, any>;

  private makespanEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner gantt-panel">
        <div class="panel-label">SCHEDULE</div>
        <div class="gantt-agent-name" id="gantt-agent-name"></div>
        ${this.navsScaffold()}
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
    this.makespanEl = document.getElementById("gantt-makespan")!;

    this.svg = select("#gantt-svg") as any;
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
    this.observeResize(wrap, resize);
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

    const x = scaleLinear().domain([0, makespan]).range([0, CHART_W]);
    const rowH = CHART_H / nMachines;
    const barH = rowH * 0.78;
    const barPad = (rowH - barH) / 2;

    const parts: string[] = [];

    for (let m = 0; m < nMachines; m++) {
      if (m % 2 === 0) {
        parts.push(`<rect x="0" y="${(m * rowH).toFixed(2)}" width="${CHART_W}" height="${rowH.toFixed(2)}" fill="${ROW_STRIPE()}"/>`);
      }
    }

    const ticks = x.ticks(8);
    for (const t of ticks) {
      const xv = x(t).toFixed(2);
      parts.push(`<line x1="${xv}" x2="${xv}" y1="0" y2="${CHART_H}" stroke="${TICK_LINE()}" stroke-width="0.5"/>`);
    }

    const barHStr = barH.toFixed(2);
    for (const bar of data.bars) {
      const bx = x(bar.start);
      const bw = Math.max(x(bar.end) - x(bar.start), 0.8);
      const by = bar.machine * rowH + barPad;
      const frac = makespan > 0 ? (bar.start / makespan).toFixed(4) : "0";
      parts.push(`<rect class="gantt-bar" style="--t:${frac}" x="${bx.toFixed(2)}" y="${by.toFixed(2)}" width="${bw.toFixed(2)}" height="${barHStr}" fill="${jobColor(bar.job)}" stroke="rgba(26,26,26,0.20)" stroke-width="0.4" rx="1"/>`);
    }

    const xMakespan = x(makespan).toFixed(2);
    parts.push(`<line x1="${xMakespan}" x2="${xMakespan}" y1="0" y2="${CHART_H}" stroke="${DANGER()}" stroke-width="1" stroke-dasharray="4,3" opacity="0.6"/>`);
    chartNode.innerHTML = parts.join("");

    const fontSize = Math.min(11, rowH * 0.55).toFixed(2);
    const labelParts: string[] = [];
    for (let m = 0; m < nMachines; m++) {
      labelParts.push(`<text x="${MARGIN.left - 5}" y="${(m * rowH + rowH / 2).toFixed(2)}" text-anchor="end" dominant-baseline="central" fill="${AXIS_TEXT()}" font-size="${fontSize}" font-family="'JetBrains Mono', monospace">${m}</text>`);
    }
    labelNode.innerHTML = labelParts.join("");

    const axisParts: string[] = [];
    for (const t of ticks) {
      const xv = x(t).toFixed(2);
      axisParts.push(`<line x1="${xv}" x2="${xv}" y1="0" y2="5" stroke="${AXIS_TEXT()}" stroke-width="0.5"/>`);
      axisParts.push(`<text x="${xv}" y="16" text-anchor="middle" fill="${AXIS_TEXT()}" font-size="9" font-family="'JetBrains Mono', monospace">${t}</text>`);
    }
    axisNode.innerHTML = axisParts.join("");

    this.makespanEl.textContent = makespan.toLocaleString();
  }
}
