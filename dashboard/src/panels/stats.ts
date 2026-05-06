import type { Panel, WSMessage } from "../types";
import { counterTween, pulseGlow } from "../lib/animate";
import { formatScore } from "../lib/format";


export class StatsPanel implements Panel {
  private agentsEl!: HTMLElement;
  private experimentsEl!: HTMLElement;
  private heroEl!: HTMLElement;
  private trackBreakdownEl!: HTMLElement;

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
          <div class="track-breakdown" id="track-breakdown"></div>
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
    this.trackBreakdownEl = document.getElementById("track-breakdown")!;
  }

  // Per-track breakdown of the swarm's best program. Only shown for the
  // global best — agents' individual track-by-track scores aren't surfaced.
  private renderTrackBreakdown(track_scores: Record<string, number> | null | undefined) {
    if (!track_scores || Object.keys(track_scores).length === 0) {
      this.trackBreakdownEl.innerHTML = "";
      this.trackBreakdownEl.classList.remove("is-visible");
      return;
    }
    const entries = Object.entries(track_scores);
    const chips = entries
      .map(
        ([track, score]) => `
        <span class="track-chip">
          <span class="track-chip-label">${escapeHTML(track)}</span>
          <span class="track-chip-value">${formatScore(score)}</span>
        </span>`,
      )
      .join("");
    this.trackBreakdownEl.innerHTML = `
      <span class="track-breakdown-label">BEST · TRACKS</span>
      ${chips}
    `;
    this.trackBreakdownEl.classList.add("is-visible");
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.agentsEl.textContent = "0";
      this.experimentsEl.textContent = "0";
      this.heroEl.textContent = "";
      this.heroEl.style.opacity = "0";
      this.heroEl.classList.remove("is-not-started");
      this.renderTrackBreakdown(null);
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
      this.renderTrackBreakdown((msg as any).track_scores);
    }
  }
}

function escapeHTML(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
