import { axisBottom, axisLeft, axisRight } from "d3-axis";
import { extent, max, min } from "d3-array";
import { scaleLinear } from "d3-scale";
import { select, type Selection } from "d3-selection";
import { line } from "d3-shape";
import { DisplayPanelBase } from "./base";
import { token } from "../lib/colors";

// Categorical assignments from the earthen viz palette:
//   discharge (energy out) → plum   (--viz-5)
//   charge    (energy in)  → olive  (--viz-3)
//   da-price line          → mustard (--viz-2)
const DISCHARGE = () => token("--viz-5", "#7A4F6E");
const CHARGE    = () => token("--viz-3", "#6B7F4E");
const PRICE     = () => token("--viz-2", "#C68F3E");
const AXIS_TEXT = () => token("--ink-dim", "rgba(26,26,26,0.50)");
const AXIS_LBL  = () => token("--ink-mid", "rgba(26,26,26,0.70)");
const ZERO_LINE = () => "rgba(26, 26, 26, 0.18)";

interface EnergyData {
  num_steps: number;
  num_batteries: number;
  agg_charge: number[];
  agg_discharge: number[];
  avg_da_price: number[];
}

type AllEnergyData = Record<string, EnergyData>;

const MARGIN = { top: 12, right: 52, bottom: 32, left: 52 };
const VB_W = 1000;
const VB_H = 500;
const CHART_W = VB_W - MARGIN.left - MARGIN.right;
const CHART_H = VB_H - MARGIN.top - MARGIN.bottom;

export class EnergyPanel extends DisplayPanelBase<AllEnergyData> {
  protected idPrefix = "energy";

  private svg!: Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: Selection<SVGGElement, unknown, HTMLElement, any>;
  private xAxisG!: Selection<SVGGElement, unknown, HTMLElement, any>;
  private yLeftAxisG!: Selection<SVGGElement, unknown, HTMLElement, any>;
  private yRightAxisG!: Selection<SVGGElement, unknown, HTMLElement, any>;

  private batteriesEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner energy-panel">
        <div class="panel-label">ENERGY SCHEDULE</div>
        <div class="energy-agent-name" id="energy-agent-name"></div>
        ${this.navsScaffold()}
        <div class="energy-svg-wrap" id="energy-svg-wrap">
          <svg id="energy-svg"></svg>
          <div class="solution-empty-state" id="energy-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="energy-batteries-box">
          <div class="solution-sub-label">BATTERIES</div>
          <div class="solution-sub-value" id="energy-batteries">---</div>
        </div>
        <div class="solution-score">
          <div class="solution-score-label">SCORE</div>
          <div class="solution-score-value" id="energy-score">---</div>
          <div class="solution-score-delta" id="energy-score-delta"></div>
        </div>
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.scoreEl = document.getElementById("energy-score")!;
    this.scoreDeltaEl = document.getElementById("energy-score-delta")!;
    this.batteriesEl = document.getElementById("energy-batteries")!;
    this.instanceLabelEl = document.getElementById("energy-instance-label")!;
    this.navEl = document.getElementById("energy-nav")!;
    this.agentNameEl = document.getElementById("energy-agent-name")!;
    this.historyNavEl = document.getElementById("energy-history-nav")!;
    this.historyLabelEl = document.getElementById("energy-history-label")!;
    this.historyLiveBtnEl = document.getElementById("energy-hist-live")!;
    this.emptyStateEl = document.getElementById("energy-empty-state")!;

    this.svg = select("#energy-svg") as any;
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
    this.observeResize(wrap, resize);
    resize();
  }

  protected onReset(): void {
    (this.chartG.node() as SVGGElement).innerHTML = "";
    this.xAxisG.selectAll("*").remove();
    this.yLeftAxisG.selectAll("*").remove();
    this.yRightAxisG.selectAll("*").remove();
    this.batteriesEl.textContent = "---";
  }

  protected showInstance(data: EnergyData) {
    const chartNode = this.chartG.node() as SVGGElement;
    chartNode.innerHTML = "";
    this.xAxisG.selectAll("*").remove();
    this.yLeftAxisG.selectAll("*").remove();
    this.yRightAxisG.selectAll("*").remove();

    if (!data || !data.agg_charge || !data.agg_charge.length) {
      this.batteriesEl.textContent = "---";
      return;
    }

    const n = data.num_steps;
    const dt = 0.25;

    const x = scaleLinear().domain([0, n * dt]).range([0, CHART_W]);

    const powerMax = Math.max(
      max(data.agg_discharge) || 0,
      Math.abs(min(data.agg_charge) || 0),
      1,
    );
    const yPower = scaleLinear()
      .domain([-powerMax * 1.1, powerMax * 1.1])
      .range([CHART_H, 0]);

    const priceExtent = extent(data.avg_da_price) as [number, number];
    const priceMin = (priceExtent[0] ?? 0) * 0.9;
    const priceMax = (priceExtent[1] ?? 100) * 1.1;
    const yPrice = scaleLinear()
      .domain([priceMin, priceMax])
      .range([CHART_H, 0]);

    // Build bars + zero-line + price-line in a single SVG string. With
    // 96 steps × 2 bar types this is ~200 elements per redraw.
    const parts: string[] = [];
    const yZero = yPower(0).toFixed(2);
    parts.push(`<line x1="0" x2="${CHART_W}" y1="${yZero}" y2="${yZero}" stroke="${ZERO_LINE()}" stroke-width="0.5"/>`);

    const barW = Math.max(0.5, CHART_W / n - 0.5).toFixed(3);
    for (let t = 0; t < n; t++) {
      const xPos = x(t * dt).toFixed(3);
      const charge = data.agg_charge[t];
      const discharge = data.agg_discharge[t];
      if (discharge > 0) {
        const yTop = yPower(discharge);
        parts.push(`<rect class="energy-bar-up" style="--t:${t}" x="${xPos}" y="${yTop.toFixed(2)}" width="${barW}" height="${(yPower(0) - yTop).toFixed(2)}" fill="${DISCHARGE()}" opacity="0.85"/>`);
      }
      if (charge < 0) {
        const yBot = yPower(charge);
        parts.push(`<rect class="energy-bar-down" style="--t:${t}" x="${xPos}" y="${yZero}" width="${barW}" height="${(yBot - yPower(0)).toFixed(2)}" fill="${CHARGE()}" opacity="0.85"/>`);
      }
    }

    if (data.avg_da_price.length > 0) {
      const priceLine = line<number>()
        .x((_, i) => x(i * dt))
        .y((d) => yPrice(d));
      const path = priceLine(data.avg_da_price);
      if (path) {
        // Sync line index `i` with bar `i`'s completion: bar i starts at
        // i*20ms and grows for 650ms, so it's done at i*20 + 650. Match
        // that pace by delaying the line by one bar-grow duration and
        // running it at 20ms per index. Keep in sync with the
        // .energy-bar-* animation rules in style.css.
        const priceDelayMs = 650;
        const priceAnimMs = Math.max(20, (n - 1) * 20);
        parts.push(`<path class="energy-price-line" style="animation-duration:${priceAnimMs}ms;animation-delay:${priceDelayMs}ms" pathLength="100" d="${path}" fill="none" stroke="${PRICE()}" stroke-width="1.5" opacity="0.95"/>`);
      }
    }
    chartNode.innerHTML = parts.join("");

    // axes
    const xTicks = axisBottom(x).ticks(8).tickFormat((d) => `${d}h`);
    this.xAxisG.call(xTicks as any)
      .selectAll("text").attr("fill", AXIS_TEXT()).attr("font-size", 11);
    this.xAxisG.selectAll("line").attr("stroke", AXIS_TEXT());
    this.xAxisG.select(".domain").attr("stroke", AXIS_TEXT());

    const yLeftTicks = axisLeft(yPower).ticks(6).tickFormat((d) => `${d}`);
    this.yLeftAxisG.call(yLeftTicks as any)
      .selectAll("text").attr("fill", AXIS_TEXT()).attr("font-size", 11);
    this.yLeftAxisG.selectAll("line").attr("stroke", AXIS_TEXT());
    this.yLeftAxisG.select(".domain").attr("stroke", AXIS_TEXT());

    this.yLeftAxisG.append("text")
      .attr("transform", "rotate(-90)")
      .attr("x", -CHART_H / 2).attr("y", -38)
      .attr("text-anchor", "middle")
      .attr("fill", AXIS_LBL())
      .attr("font-size", 11)
      .text("MW");

    const yRightTicks = axisRight(yPrice).ticks(6).tickFormat((d) => `$${d}`);
    this.yRightAxisG.call(yRightTicks as any)
      .selectAll("text").attr("fill", PRICE()).attr("font-size", 11);
    this.yRightAxisG.selectAll("line").attr("stroke", PRICE()).attr("opacity", 0.4);
    this.yRightAxisG.select(".domain").attr("stroke", PRICE()).attr("opacity", 0.4);

    this.yRightAxisG.append("text")
      .attr("transform", "rotate(90)")
      .attr("x", CHART_H / 2).attr("y", -40)
      .attr("text-anchor", "middle")
      .attr("fill", PRICE())
      .attr("font-size", 11)
      .text("$/MWh");

    // legend
    const legendY = -2;
    this.chartG.append("rect")
      .attr("x", 4).attr("y", legendY).attr("width", 10).attr("height", 10)
      .attr("fill", DISCHARGE()).attr("opacity", 0.85);
    this.chartG.append("text")
      .attr("x", 18).attr("y", legendY + 9)
      .attr("fill", AXIS_LBL()).attr("font-size", 11).text("Discharge");

    this.chartG.append("rect")
      .attr("x", 84).attr("y", legendY).attr("width", 10).attr("height", 10)
      .attr("fill", CHARGE()).attr("opacity", 0.85);
    this.chartG.append("text")
      .attr("x", 98).attr("y", legendY + 9)
      .attr("fill", AXIS_LBL()).attr("font-size", 11).text("Charge");

    this.chartG.append("line")
      .attr("x1", 152).attr("x2", 162).attr("y1", legendY + 5).attr("y2", legendY + 5)
      .attr("stroke", PRICE()).attr("stroke-width", 1.5);
    this.chartG.append("text")
      .attr("x", 166).attr("y", legendY + 9)
      .attr("fill", AXIS_LBL()).attr("font-size", 11).text("DA Price");

    this.batteriesEl.textContent = String(data.num_batteries);
  }
}
