import { getAgentColor } from "../../lib/colors";
import type { Panel, WSMessage } from "../../types";
import { getViewedChallenge } from "../../lib/viewedChallenge";
import { getDashboardUrls } from "../../lib/bootstrap";
import { heatmapCorner, heatmapShortName, ThrottledRefresh } from "../../lib/heatmap";

interface InspirationData {
  agents: { agent_id: string; agent_name: string }[];
  matrix: number[][];
}

export class InspirationMatrixPanel implements Panel {
  private inner!: HTMLElement;
  private apiUrl = "";
  private throttle = new ThrottledRefresh(() => this.fetchAndRender());
  private renderedChallenge = "";

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="panel-inner diversity-panel">
        <div class="panel-label">INSPIRATION MATRIX · TRAJECTORIES</div>
        <div class="diversity-grid" id="inspiration-grid"></div>
      </div>
    `;
    this.inner = document.getElementById("inspiration-grid")!;

    this.apiUrl = getDashboardUrls().apiUrl;

    this.fetchAndRender();
  }

  setChallenge(_c: string) {
    // Cancel any pending throttled fetch from the previous challenge. Without
    // this, a timer queued just before the switch fires after the switch and
    // triggers a redundant fetch (it self-corrects via the getViewedChallenge
    // check inside fetchAndRender, but the round-trip is still wasted).
    this.throttle.cancel();
    this.throttle.forceNext();
    this.fetchAndRender();
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.inner.innerHTML = "";
      this.renderedChallenge = "";
      return;
    }
    if (msg.type !== "leaderboard_update") return;
    this.throttle.request();
  }

  private async fetchAndRender() {
    this.throttle.markRun();
    const ch = getViewedChallenge();
    try {
      const res = await fetch(
        `${this.apiUrl}/api/inspiration_matrix?challenge=${encodeURIComponent(ch)}`,
      );
      if (!res.ok) return;
      const data: InspirationData = await res.json();
      if (ch !== getViewedChallenge()) return;
      this.renderedChallenge = ch;
      this.render(data);
    } catch {
      // silently retry on next update
    }
  }

  private render(data: InspirationData) {
    const { agents, matrix } = data;
    if (!agents.length) {
      this.inner.innerHTML = `<span style="color:var(--text-dim);font-size:11px">No inspiration events yet</span>`;
      return;
    }

    let maxVal = 0;
    const n = agents.length;
    for (let i = 0; i < n; i++)
      for (let j = 0; j < n; j++)
        if (i !== j && matrix[i][j] > maxVal) maxVal = matrix[i][j];

    const grid = document.createElement("div");
    grid.className = "dv-grid";
    grid.style.gridTemplateColumns = `56px repeat(${n}, 1fr)`;
    grid.style.gridTemplateRows = `20px repeat(${n}, 1fr)`;

    grid.appendChild(heatmapCorner());
    for (let j = 0; j < n; j++) {
      const hdr = document.createElement("div");
      hdr.className = "dv-col-hdr";
      hdr.style.color = getAgentColor(agents[j].agent_id);
      hdr.textContent = heatmapShortName(agents[j].agent_name);
      hdr.title = `${agents[j].agent_name} (source trajectory)`;
      grid.appendChild(hdr);
    }

    for (let i = 0; i < n; i++) {
      const rh = document.createElement("div");
      rh.className = "dv-row-hdr";
      rh.style.color = getAgentColor(agents[i].agent_id);
      rh.textContent = heatmapShortName(agents[i].agent_name);
      rh.title = `${agents[i].agent_name} (receiver trajectory)`;
      grid.appendChild(rh);

      for (let j = 0; j < n; j++) {
        const val = matrix[i][j];
        const cell = document.createElement("div");
        cell.className = i === j ? "dv-cell dv-diag" : "dv-cell";
        cell.style.background = i === j
          ? "rgba(26, 26, 26, 0.04)"
          : this.cellColor(val, maxVal);
        cell.textContent = String(val);
        cell.title = i === j
          ? `${agents[i].agent_name} (self)`
          : `${agents[i].agent_name} inspired by ${agents[j].agent_name}: ${val} time${val !== 1 ? "s" : ""}`;
        grid.appendChild(cell);
      }
    }

    this.inner.innerHTML = "";
    this.inner.appendChild(grid);
  }

  private cellColor(val: number, maxVal: number): string {
    if (maxVal === 0 || val === 0) return "rgba(78, 107, 133, 0.05)";
    const a = Math.max(0.08, (val / maxVal) * 0.75);
    return `rgba(78, 107, 133, ${a})`;
  }
}
