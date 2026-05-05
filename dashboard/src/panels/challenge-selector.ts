// Challenge selector dropdown — visible at the top of every dashboard page.
//
// The selector lets the dashboard user choose which challenge's leaderboard,
// feed, ideas, and visualization to view. The active challenge (the one
// agents are currently working on) is marked with a "● active" badge so
// viewers know which one is live; other challenges show their historical
// state (leaderboard / feed from the last time agents worked on them).
//
// Selection is persisted to localStorage so the choice sticks across pages
// and reloads. Changing the selector triggers `setViewedChallenge`, which
// fans out to every panel via `onViewedChallengeChange` listeners.

import type { Panel, WSMessage } from "../types";
import {
  getActiveChallenge,
  getAvailableChallenges,
  onSwarmConfigChange,
  type Challenge,
} from "../lib/swarmConfig";
import {
  getViewedChallenge,
  setViewedChallenge,
  onViewedChallengeChange,
} from "../lib/viewedChallenge";

const PRETTY: Record<string, string> = {
  satisfiability: "Satisfiability",
  vehicle_routing: "Vehicle Routing",
  knapsack: "Knapsack",
  job_scheduling: "Job Scheduling",
  energy_arbitrage: "Energy Arbitrage",
};

export class ChallengeSelectorPanel implements Panel {
  private container!: HTMLElement;
  private select!: HTMLSelectElement;
  private badge!: HTMLElement;
  private unsubSwarm: (() => void) | null = null;
  private unsubViewed: (() => void) | null = null;

  init(container: HTMLElement) {
    this.container = container;
    container.innerHTML = `
      <div class="challenge-selector">
        <label for="challenge-select" class="challenge-selector-label">Viewing:</label>
        <select id="challenge-select" class="challenge-selector-dropdown"></select>
        <span class="challenge-active-badge" id="challenge-active-badge"></span>
      </div>
    `;
    this.select = container.querySelector<HTMLSelectElement>("#challenge-select")!;
    this.badge = container.querySelector<HTMLElement>("#challenge-active-badge")!;
    this.render();
    this.select.addEventListener("change", () => {
      setViewedChallenge(this.select.value as Challenge);
    });
    this.unsubSwarm = onSwarmConfigChange(() => this.render());
    this.unsubViewed = onViewedChallengeChange(() => this.render());
  }

  handleMessage(_msg: WSMessage): void {
    // No-op — the selector reacts to swarm_config_updated via the
    // onSwarmConfigChange listener wired in init().
  }

  dispose(): void {
    this.unsubSwarm?.();
    this.unsubViewed?.();
  }

  private render(): void {
    const available = getAvailableChallenges();
    const active = getActiveChallenge();
    const viewed = getViewedChallenge();
    const previous = this.select.value;
    this.select.innerHTML = "";
    for (const ch of available) {
      const opt = document.createElement("option");
      opt.value = ch;
      opt.textContent = (PRETTY[ch] ?? ch) + (ch === active ? " (active)" : "");
      this.select.appendChild(opt);
    }
    this.select.value = available.includes(viewed) ? viewed : (available[0] ?? "");
    if (this.select.value !== previous && this.select.value !== viewed) {
      // Selector ended up on a different value because the viewed challenge
      // disappeared from `available` — update viewedChallenge to match.
      setViewedChallenge(this.select.value as Challenge);
    }
    this.badge.textContent = viewed === active ? "● live" : "○ historical";
    this.badge.className = `challenge-active-badge ${viewed === active ? "is-active" : "is-historical"}`;
  }
}
