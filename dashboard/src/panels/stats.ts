import type { Panel, WSMessage } from "../types";
import { counterTween, pulseGlow } from "../lib/animate";
import { formatScore } from "../lib/format";


export class StatsPanel implements Panel {
  private agentsEl!: HTMLElement;
  private experimentsEl!: HTMLElement;
  private heroEl!: HTMLElement;
  // Latest track_scores for the global best of the viewed challenge. Rendered
  // on demand into the solution panel's `.routes-score` block when the user
  // clicks the score; nothing is shown in the stats bar itself.
  private trackScores: Record<string, number> | null = null;
  private clickAttached = false;

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="stats-bar">
        <div class="stats-logo">
          <span class="stats-diamond">&#9670;</span>
          <span class="stats-title">Automated Discovery</span>
          <span id="ws-status" class="ws-status connected">LIVE</span>
          <a href="/ideas.html" class="stats-nav-link">Ideas &rarr;</a>
          <a href="/diversity.html" class="stats-nav-link">Diversity &rarr;</a>
          <a href="/benchmark.html" class="stats-nav-link">Benchmark &rarr;</a>
          <a href="/trajectories.html" class="stats-nav-link">Trajectories &rarr;</a>
        </div>
        <div class="stats-chips">
          <div class="stat-chip" id="stat-agents">
            <span class="stat-label">ACTIVE</span>
            <span class="stat-value" id="stat-agents-val">0</span>
          </div>
          <div class="stat-chip" id="stat-experiments">
            <span class="stat-label">EXPERIMENTS</span>
            <span class="stat-value" id="stat-experiments-val">0</span>
          </div>
          <div class="stat-hero" id="stat-hero"></div>
        </div>
      </div>
    `;

    this.agentsEl = document.getElementById("stat-agents-val")!;
    this.experimentsEl = document.getElementById("stat-experiments-val")!;
    this.heroEl = document.getElementById("stat-hero")!;

    this.attachScoreClick();
  }

  // Wire a click handler on the active solution panel's `.routes-score-value`
  // so clicking the big number toggles the track-score breakdown. Solution
  // panels initialise after stats, so retry briefly until the element exists.
  private attachScoreClick(retries = 20) {
    if (this.clickAttached) return;
    const scoreParent = document.querySelector(".routes-score") as HTMLElement | null;
    const scoreVal = scoreParent?.querySelector(".routes-score-value") as HTMLElement | null;
    if (!scoreParent || !scoreVal) {
      if (retries > 0) setTimeout(() => this.attachScoreClick(retries - 1), 100);
      return;
    }
    this.clickAttached = true;
    scoreVal.style.cursor = "pointer";
    scoreVal.title = "Click to show per-track scores";
    scoreVal.addEventListener("click", () => {
      scoreParent.classList.toggle("routes-score--expanded");
    });
    this.renderTrackBreakdown();
  }

  // Per-track breakdown of the swarm's best program. Only shown for the
  // global best — agents' individual track-by-track scores aren't surfaced.
  // Injected into the solution panel's `.routes-score` block as a popover
  // that stays hidden until the user clicks the score.
  private renderTrackBreakdown() {
    const scoreParent = document.querySelector(".routes-score") as HTMLElement | null;
    if (!scoreParent) return;
    let host = scoreParent.querySelector(".track-breakdown") as HTMLElement | null;
    if (!this.trackScores || Object.keys(this.trackScores).length === 0) {
      if (host) host.innerHTML = "";
      scoreParent.classList.remove("routes-score--expanded");
      return;
    }
    if (!host) {
      host = document.createElement("div");
      host.className = "track-breakdown";
      scoreParent.appendChild(host);
    }
    const chips = Object.entries(this.trackScores)
      .map(
        ([track, score]) => `
        <span class="track-chip">
          <span class="track-chip-label">${escapeHTML(track)}</span>
          <span class="track-chip-value">${formatScore(score)}</span>
        </span>`,
      )
      .join("");
    host.innerHTML = `
      <span class="track-breakdown-label">BEST · TRACKS</span>
      ${chips}
    `;
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.agentsEl.textContent = "0";
      this.experimentsEl.textContent = "0";
      this.heroEl.textContent = "";
      this.heroEl.style.opacity = "0";
      this.heroEl.classList.remove("is-not-started");
      this.trackScores = null;
      this.renderTrackBreakdown();
      return;
    }

    if (msg.type === "stats_update") {
      // ACTIVE = agents currently working on the viewed challenge (not the
      // global swarm size). main.ts slices the per_challenge map so
      // `msg.active_agents` is per-challenge after the filter.
      counterTween(this.agentsEl, msg.active_agents);
      counterTween(this.experimentsEl, msg.total_experiments);

      // "Not started" hero overlay when the viewed challenge has no data.
      // Cleared as soon as anything lands.
      const notStarted =
        (msg.total_experiments ?? 0) === 0 &&
        (msg.best_score === null || msg.best_score === undefined);
      if (notStarted) {
        this.heroEl.textContent = "Not started";
        this.heroEl.classList.add("is-not-started");
        this.heroEl.style.opacity = "1";
      } else if (this.heroEl.classList.contains("is-not-started")) {
        // First non-empty update — clear the placeholder so live hero
        // names from new_global_best events can render.
        this.heroEl.classList.remove("is-not-started");
        this.heroEl.textContent = "";
        this.heroEl.style.opacity = "0";
      }
    }

    if (msg.type === "agent_joined") {
      pulseGlow(document.getElementById("stat-agents")!);
    }

    if (msg.type === "experiment_published") {
      pulseGlow(document.getElementById("stat-experiments")!);
    }

    if (msg.type === "new_global_best") {
      this.heroEl.classList.remove("is-not-started");
      this.heroEl.textContent = msg.agent_name;
      this.heroEl.style.opacity = "1";
      setTimeout(() => {
        this.heroEl.style.opacity = "0";
      }, 5000);
      this.trackScores = (msg as any).track_scores ?? null;
      this.renderTrackBreakdown();
      this.attachScoreClick();
    }
  }
}

function escapeHTML(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
