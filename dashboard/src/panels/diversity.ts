import { getAgentColor } from "../lib/colors";
import type { Panel, WSMessage } from "../types";
import { getViewedChallenge } from "../lib/viewedChallenge";

interface DiversityData {
  agents: { agent_id: string; agent_name: string }[];
  matrix: number[][];
}

export class DiversityPanel implements Panel {
  private container!: HTMLElement;
  private inner!: HTMLElement;
  private apiUrl = "";
  private throttleTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFetch = 0;
  // Tracks the challenge whose data is currently in `inner`. Used to detect
  // a viewed-challenge switch so we can drop stale rows immediately rather
  // than letting the previous matrix linger until the next fetch.
  private renderedChallenge = "";
  private static THROTTLE_MS = 30_000;
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

    const wsEl = document.querySelector(".ws-status");
    if (wsEl) {
      const proto = window.location.protocol;
      this.apiUrl = `${proto}//${window.location.host}`;
    }
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
      }
    }

    this.fetchAndRender();
  }

  setChallenge(_c: string) {
    // main.ts dispatches a `reset` to every panel before invoking
    // setChallenge. We fetch here (not in reset) so the inner is empty
    // while the new challenge's matrix is in flight, then refetched
    // against the now-current viewed challenge.
    this.lastFetch = 0;
    this.fetchAndRender();
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.inner.innerHTML = "";
      this.renderedChallenge = "";
      return;
    }
    if (msg.type !== "leaderboard_update") return;

    const elapsed = Date.now() - this.lastFetch;
    if (elapsed >= DiversityPanel.THROTTLE_MS) {
      this.fetchAndRender();
    } else if (!this.throttleTimer) {
      this.throttleTimer = setTimeout(() => {
        this.throttleTimer = null;
        this.fetchAndRender();
      }, DiversityPanel.THROTTLE_MS - elapsed);
    }
  }

  private async fetchAndRender() {
    this.lastFetch = Date.now();
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
    const { agents, matrix } = data;
    if (!agents.length) {
      this.inner.innerHTML = `<span style="color:var(--text-dim);font-size:11px">No agents yet</span>`;
      return;
    }

    const n = agents.length;
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
    grid.appendChild(this.corner());
    for (let j = 0; j < n; j++) {
      const hdr = document.createElement("div");
      hdr.className = "dv-col-hdr";
      hdr.style.color = getAgentColor(agents[j].agent_id);
      hdr.textContent = this.shortName(agents[j].agent_name);
      hdr.title = agents[j].agent_name;
      grid.appendChild(hdr);
    }

    // Rows
    for (let i = 0; i < n; i++) {
      // Row header
      const rh = document.createElement("div");
      rh.className = "dv-row-hdr";
      rh.style.color = getAgentColor(agents[i].agent_id);
      rh.textContent = this.shortName(agents[i].agent_name);
      rh.title = agents[i].agent_name;
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
          ? `${agents[i].agent_name}: ${(val * 100).toFixed(1)}% unique lines`
          : `${(val * 100).toFixed(1)}% of ${agents[i].agent_name}'s lines found in ${agents[j].agent_name}`;
        grid.appendChild(cell);
      }
    }

    this.inner.innerHTML = "";
    this.inner.appendChild(grid);
  }

  private corner(): HTMLElement {
    const el = document.createElement("div");
    el.className = "dv-corner";
    return el;
  }

  private shortName(name: string): string {
    // The server now labels rows as "<traj-id> · <agent-name>(possibly · inactive)".
    // The traj-id prefix is what the operator scans for, so keep it intact
    // and truncate from the trailing agent-name half when the label is too
    // long for the heatmap chip.
    if (name.length <= 12) return name;
    const dot = " · ";
    const idx = name.indexOf(dot);
    if (idx < 0 || idx >= 10) return name.slice(0, 11) + "…";
    const head = name.slice(0, idx); // traj-id
    const tail = name.slice(idx + dot.length);
    const tailBudget = Math.max(1, 12 - head.length - dot.length);
    return tail.length <= tailBudget
      ? `${head}${dot}${tail}`
      : `${head}${dot}${tail.slice(0, tailBudget)}…`;
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
