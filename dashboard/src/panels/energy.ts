import * as d3 from "d3";
import { DisplayPanelBase } from "./displayPanelBase";

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

  private svg!: d3.Selection<SVGSVGElement, unknown, HTMLElement, any>;
  private chartG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private xAxisG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private yLeftAxisG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;
  private yRightAxisG!: d3.Selection<SVGGElement, unknown, HTMLElement, any>;

  private batteriesEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner energy-panel">
        <div class="panel-label">ENERGY SCHEDULE</div>
        <div class="energy-agent-name" id="energy-agent-name"></div>
        <div class="solution-history-nav" id="energy-history-nav" style="display:none">
          <button class="solution-nav-btn" id="energy-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="solution-history-label" id="energy-history-label"></span>
          <button class="solution-nav-btn" id="energy-hist-next" title="Next global best">&rsaquo;</button>
          <button class="solution-history-live" id="energy-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="solution-nav" id="energy-nav" style="display:none">
          <button class="solution-nav-btn" id="energy-prev">&lsaquo;</button>
          <span class="solution-instance-label" id="energy-instance-label"></span>
          <button class="solution-nav-btn" id="energy-next">&rsaquo;</button>
        </div>
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

    // Build bars + zero-line + price-line in a single SVG string. With
    // 96 steps × 2 bar types this is ~200 elements per redraw.
    const parts: string[] = [];
    const yZero = yPower(0).toFixed(2);
    parts.push(`<line x1="0" x2="${CHART_W}" y1="${yZero}" y2="${yZero}" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>`);

    const barW = Math.max(0.5, CHART_W / n - 0.5).toFixed(3);
    for (let t = 0; t < n; t++) {
      const xPos = x(t * dt).toFixed(3);
      const charge = data.agg_charge[t];
      const discharge = data.agg_discharge[t];
      if (discharge > 0) {
        const yTop = yPower(discharge);
        parts.push(`<rect x="${xPos}" y="${yTop.toFixed(2)}" width="${barW}" height="${(yPower(0) - yTop).toFixed(2)}" fill="#ef5350" opacity="0.8"/>`);
      }
      if (charge < 0) {
        const yBot = yPower(charge);
        parts.push(`<rect x="${xPos}" y="${yZero}" width="${barW}" height="${(yBot - yPower(0)).toFixed(2)}" fill="#42a5f5" opacity="0.8"/>`);
      }
    }

    if (data.avg_da_price.length > 0) {
      const priceLine = d3.line<number>()
        .x((_, i) => x(i * dt))
        .y((d) => yPrice(d));
      const path = priceLine(data.avg_da_price);
      if (path) {
        parts.push(`<path d="${path}" fill="none" stroke="#ffd740" stroke-width="1.5" opacity="0.9"/>`);
      }
    }
    chartNode.innerHTML = parts.join("");

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
