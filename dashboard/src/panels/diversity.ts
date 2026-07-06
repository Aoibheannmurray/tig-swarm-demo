import { getAgentColor } from "../lib/colors";
import type { Panel, WSMessage } from "../types";
import { getViewedChallenge } from "../lib/viewedChallenge";
import { getDashboardUrls } from "../lib/bootstrap";
import { heatmapCorner, heatmapShortName, ThrottledRefresh } from "../lib/heatmap";

interface DiversityData {
  trajectories: { trajectory_id: string; display_name: string }[];
  matrix: number[][];
}

export class DiversityPanel implements Panel {
  private container!: HTMLElement;
  private inner!: HTMLElement;
  private apiUrl = "";
  private throttle = new ThrottledRefresh(() => this.fetchAndRender());
  // Tracks the challenge whose data is currently in `inner`. Used to detect
  // a viewed-challenge switch so we can drop stale rows immediately rather
  // than letting the previous matrix linger until the next fetch.
  private renderedChallenge = "";
  // Above this many trajectories, stop shrinking cells to fit and let the
  // grid overflow horizontally/vertically inside its scroll container.
  private static SCROLL_THRESHOLD = 20;
  private static FIXED_CELL_PX = 24;
  private static ROW_HDR_PX = 56;
  private static COL_HDR_PX = 20;

  init(container: HTMLElement) {
    this.container = container;
    container.innerHTML = `
      <div class="panel-inner diversity-panel">
        <div class="panel-label">CODE DIVERSITY · TRAJECTORIES</div>
        <div class="diversity-grid" id="diversity-grid"></div>
      </div>
    `;
    this.inner = document.getElementById("diversity-grid")!;

    this.apiUrl = getDashboardUrls().apiUrl;

    this.fetchAndRender();
  }

  setChallenge(_c: string) {
    // main.ts dispatches a `reset` to every panel before invoking
    // setChallenge. We fetch here (not in reset) so the inner is empty
    // while the new challenge's matrix is in flight, then refetched
    // against the now-current viewed challenge.
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
    // Always scope the matrix to the viewed challenge — the server
    // endpoint defaults to the active challenge otherwise, which would
    // show e.g. the energy_arbitrage matrix while the user is viewing
    // VRP.
    const ch = getViewedChallenge();
    try {
      const res = await fetch(
        `${this.apiUrl}/api/diversity?challenge=${encodeURIComponent(ch)}`,
      );
      if (!res.ok) return;
      const data: DiversityData = await res.json();
      // Discard a stale response if the user has already switched to a
      // different challenge while this request was in flight.
      if (ch !== getViewedChallenge()) return;
      this.renderedChallenge = ch;
      this.render(data);
    } catch {
      // silently retry on next update
    }
  }

  private render(data: DiversityData) {
    const { trajectories, matrix } = data;
    if (!trajectories.length) {
      this.inner.innerHTML = `<span style="color:var(--text-dim);font-size:11px">No trajectories yet</span>`;
      return;
    }

    const n = trajectories.length;
    const grid = document.createElement("div");
    grid.className = "dv-grid";
    const scrollMode = n > DiversityPanel.SCROLL_THRESHOLD;
    if (scrollMode) {
      // Past the threshold, freeze each cell at FIXED_CELL_PX and let the
      // grid grow beyond its container so the .diversity-grid scroll
      // container shows a horizontal (and vertical) scrollbar.
      const cell = `${DiversityPanel.FIXED_CELL_PX}px`;
      grid.style.gridTemplateColumns = `${DiversityPanel.ROW_HDR_PX}px repeat(${n}, ${cell})`;
      grid.style.gridTemplateRows = `${DiversityPanel.COL_HDR_PX}px repeat(${n}, ${cell})`;
      grid.style.width = "max-content";
      grid.style.maxWidth = "none";
      grid.style.height = "max-content";
      grid.style.maxHeight = "none";
    } else {
      grid.style.gridTemplateColumns = `${DiversityPanel.ROW_HDR_PX}px repeat(${n}, 1fr)`;
      grid.style.gridTemplateRows = `${DiversityPanel.COL_HDR_PX}px repeat(${n}, 1fr)`;
    }
    this.inner.classList.toggle("diversity-grid--scroll", scrollMode);

    // Column headers
    grid.appendChild(heatmapCorner());
    for (let j = 0; j < n; j++) {
      const hdr = document.createElement("div");
      hdr.className = "dv-col-hdr";
      hdr.style.color = getAgentColor(trajectories[j].trajectory_id);
      hdr.textContent = heatmapShortName(trajectories[j].display_name);
      hdr.title = trajectories[j].display_name;
      grid.appendChild(hdr);
    }

    // Rows
    for (let i = 0; i < n; i++) {
      // Row header
      const rh = document.createElement("div");
      rh.className = "dv-row-hdr";
      rh.style.color = getAgentColor(trajectories[i].trajectory_id);
      rh.textContent = heatmapShortName(trajectories[i].display_name);
      rh.title = trajectories[i].display_name;
      grid.appendChild(rh);

      for (let j = 0; j < n; j++) {
        const val = matrix[i][j];
        const cell = document.createElement("div");
        cell.className = i === j ? "dv-cell dv-diag" : "dv-cell";
        cell.style.background = i === j
          ? this.diagColor(val)
          : this.cellColor(val);
        cell.textContent = (val * 100).toFixed(0);
        cell.title = i === j
          ? `${trajectories[i].display_name}: ${(val * 100).toFixed(1)}% unique lines`
          : `${(val * 100).toFixed(1)}% of ${trajectories[i].display_name}'s lines found in ${trajectories[j].display_name}`;
        grid.appendChild(cell);
      }
    }

    this.inner.innerHTML = "";
    this.inner.appendChild(grid);
  }

  private cellColor(val: number): string {
    // 0 = pale cream, 1 = saturated terracotta (similarity heat)
    const a = Math.max(0.05, val * 0.7);
    return `rgba(184, 84, 31, ${a})`;
  }

  private diagColor(val: number): string {
    // 0 = pale cream, 1 = saturated mustard (uniqueness)
    const a = Math.max(0.05, val * 0.8);
    return `rgba(198, 143, 62, ${a})`;
  }
}
