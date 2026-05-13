import { select } from "d3-selection";
import { curveCatmullRom, line } from "d3-shape";
import { getRouteColor } from "../lib/colors";
import { DisplayPanelBase } from "./base";

interface RoutePoint {
  x: number;
  y: number;
  customer_id: number;
}

interface RouteData {
  depot: { x: number; y: number };
  routes: { vehicle_id: number; path: RoutePoint[] }[];
}

// solution_data from server: dict keyed by instance name.
type AllRouteData = Record<string, RouteData>;

const STYLE = {
  customerRadius: 0.006,
  customerStroke: 0.0015,
  depotSize:      0.020,
  routeStroke:    0.0055,
  glowStroke:     0.012,
  routeDashOn:    0.018,
  routeDashOff:   0.007,
} as const;

const routeLine = line<RoutePoint>()
  .x((d) => d.x)
  .y((d) => d.y)
  .curve(curveCatmullRom.alpha(0.5));

function fullPath(data: RouteData, route: { path: RoutePoint[] }): RoutePoint[] {
  const depot = { x: data.depot.x, y: data.depot.y, customer_id: -1 };
  return [depot, ...route.path, depot];
}

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

export class SolutionPanel extends DisplayPanelBase<AllRouteData> {
  protected idPrefix = "solution";

  private svg!: any;
  private routeGroup!: any;
  private customerGroup!: any;
  private depotGroup!: any;
  private routeDistanceEl!: HTMLElement;

  // Side length of the current viewBox in SVG user units. All draw sizes
  // are computed as STYLE.* × viewSide so they stay visually consistent
  // regardless of how spread out the underlying data is.
  private viewSide = 1000;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner solution-panel">
        <div class="panel-label">ROUTES</div>
        <div class="solution-agent-name" id="solution-agent-name"></div>
        ${this.navsScaffold()}
        <div class="solution-svg-wrap" id="solution-svg-wrap">
          <svg id="solution-svg"></svg>
          <div class="solution-empty-state" id="solution-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
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
  }

  protected attachRefs(_root: HTMLElement): void {
    this.scoreEl = document.getElementById("solution-score")!;
    this.scoreDeltaEl = document.getElementById("solution-score-delta")!;
    this.routeDistanceEl = document.getElementById("solution-route-distance")!;
    this.instanceLabelEl = document.getElementById("solution-instance-label")!;
    this.navEl = document.getElementById("solution-nav")!;
    this.agentNameEl = document.getElementById("solution-agent-name")!;
    this.historyNavEl = document.getElementById("solution-history-nav")!;
    this.historyLabelEl = document.getElementById("solution-history-label")!;
    this.historyLiveBtnEl = document.getElementById("solution-hist-live")!;
    this.emptyStateEl = document.getElementById("solution-empty-state")!;

    this.svg = select("#solution-svg");
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

    const wrap = document.getElementById("solution-svg-wrap")!;
    const resize = () => {
      const size = Math.max(0, Math.min(wrap.clientWidth, wrap.clientHeight));
      this.svg.attr("width", size).attr("height", size);
    };
    this.observeResize(wrap, resize);
    resize();
  }

  protected onReset(): void {
    (this.routeGroup.node() as SVGGElement).innerHTML = "";
    (this.customerGroup.node() as SVGGElement).innerHTML = "";
    (this.depotGroup.node() as SVGGElement).innerHTML = "";
    this.svg.attr("viewBox", "0 0 1000 1000");
    this.viewSide = 1000;
    this.routeDistanceEl.textContent = "---";
  }

  protected onAfterApplyHistory(): void {
    this.updateViewBox();
  }

  // Compute a square viewBox that tightly bounds *all* instances' data.
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

  protected showInstance(data: RouteData) {
    const routeNode = this.routeGroup.node() as SVGGElement;
    const customerNode = this.customerGroup.node() as SVGGElement;
    const depotNode = this.depotGroup.node() as SVGGElement;

    const s = this.viewSide;
    const customerR = (STYLE.customerRadius * s).toFixed(3);
    const customerStroke = (STYLE.customerStroke * s).toFixed(3);
    const routeW = (STYLE.routeStroke * s).toFixed(3);
    const glowW = (STYLE.glowStroke * s).toFixed(3);
    const dashOn = (STYLE.routeDashOn * s).toFixed(3);
    const dashOff = (STYLE.routeDashOff * s).toFixed(3);

    const routeParts: string[] = [];
    const customerParts: string[] = [];
    data.routes.forEach((route, i) => {
      const path = fullPath(data, route);
      const color = getRouteColor(i);
      const d = routeLine(path);
      if (!d) return;

      routeParts.push(`<path d="${d}" fill="none" stroke="#fff" stroke-width="${glowW}" stroke-opacity="0.45" filter="url(#route-glow)"/>`);
      routeParts.push(`<path d="${d}" fill="none" stroke="${color}" stroke-width="${routeW}" stroke-dasharray="${dashOn} ${dashOff}" class="route-flowing"/>`);

      for (const pt of route.path) {
        customerParts.push(`<circle cx="${pt.x}" cy="${pt.y}" r="${customerR}" fill="${color}" stroke="#1A1A1A" stroke-width="${customerStroke}"/>`);
      }
    });
    routeNode.innerHTML = routeParts.join("");
    customerNode.innerHTML = customerParts.join("");

    const depotSize = STYLE.depotSize * s;
    depotNode.innerHTML = `<rect x="${data.depot.x - depotSize / 2}" y="${data.depot.y - depotSize / 2}" width="${depotSize}" height="${depotSize}" fill="#1A1A1A" opacity="0.9" transform="rotate(45, ${data.depot.x}, ${data.depot.y})" class="depot-pulse"/>`;

    this.routeDistanceEl.textContent = computeRouteDistance(data).toFixed(1);
  }
}
