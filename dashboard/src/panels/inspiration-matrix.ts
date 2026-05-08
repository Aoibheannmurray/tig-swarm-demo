import { getAgentColor } from "../lib/colors";
import type { Panel, WSMessage } from "../types";
import { getViewedChallenge } from "../lib/viewedChallenge";

interface InspirationData {
  agents: { agent_id: string; agent_name: string }[];
  matrix: number[][];
}

export class InspirationMatrixPanel implements Panel {
  private inner!: HTMLElement;
  private apiUrl = "";
  private throttleTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFetch = 0;
  private renderedChallenge = "";
  private static THROTTLE_MS = 30_000;

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="panel-inner diversity-panel">
        <div class="panel-label">INSPIRATION MATRIX</div>
        <div class="diversity-grid" id="inspiration-grid"></div>
      </div>
    `;
    this.inner = document.getElementById("inspiration-grid")!;

    const params = new URLSearchParams(window.location.search);
    const explicit = params.get("api");
    if (explicit) {
      this.apiUrl = explicit;
    } else {
      const ws = params.get("ws") || "";
      if (ws) {
        this.apiUrl = ws
          .replace("ws://", "http://")
          .replace("wss://", "https://")
          .replace("/ws/dashboard", "");
      } else {
        const proto = window.location.protocol;
        this.apiUrl = `${proto}//${window.location.host}`;
      }
    }

    this.fetchAndRender();
  }

  setChallenge(_c: string) {
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
    if (elapsed >= InspirationMatrixPanel.THROTTLE_MS) {
      this.fetchAndRender();
    } else if (!this.throttleTimer) {
      this.throttleTimer = setTimeout(() => {
        this.throttleTimer = null;
        this.fetchAndRender();
      }, InspirationMatrixPanel.THROTTLE_MS - elapsed);
    }
  }

  private async fetchAndRender() {
    this.lastFetch = Date.now();
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

    grid.appendChild(this.corner());
    for (let j = 0; j < n; j++) {
      const hdr = document.createElement("div");
      hdr.className = "dv-col-hdr";
      hdr.style.color = getAgentColor(agents[j].agent_id);
      hdr.textContent = this.shortName(agents[j].agent_name);
      hdr.title = `${agents[j].agent_name} (source)`;
      grid.appendChild(hdr);
    }

    for (let i = 0; i < n; i++) {
      const rh = document.createElement("div");
      rh.className = "dv-row-hdr";
      rh.style.color = getAgentColor(agents[i].agent_id);
      rh.textContent = this.shortName(agents[i].agent_name);
      rh.title = `${agents[i].agent_name} (receiver)`;
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
          : `${agents[i].agent_name} received inspiration from ${agents[j].agent_name}: ${val} time${val !== 1 ? "s" : ""}`;
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
    if (name.length <= 10) return name;
    return name.slice(0, 9) + "…";
  }

  private cellColor(val: number, maxVal: number): string {
    if (maxVal === 0 || val === 0) return "rgba(78, 107, 133, 0.05)";
    const a = Math.max(0.08, (val / maxVal) * 0.75);
    return `rgba(78, 107, 133, ${a})`;
  }
}
