import type { Panel, WSMessage } from "../types";
import { counterTween, pulseGlow } from "../lib/animate";
import { formatScore } from "../lib/format";
import { getViewedChallenge, onViewedChallengeChange } from "../lib/viewedChallenge";


// Resolve API base URL the same way other panels do (chart.ts, gantt.ts).
function resolveApiUrl(): string {
  const params = new URLSearchParams(window.location.search);
  const explicit = params.get("api");
  if (explicit) return explicit;
  const ws = params.get("ws") || "";
  if (ws) {
    return ws
      .replace("ws://", "http://")
      .replace("wss://", "https://")
      .replace("/ws/dashboard", "");
  }
  return `${window.location.protocol}//${window.location.host}`;
}

function resolveViewedChallenge(): string {
  return getViewedChallenge();
}


export class StatsPanel implements Panel {
  private agentsEl!: HTMLElement;
  private experimentsEl!: HTMLElement;
  private heroEl!: HTMLElement;
  // Latest track_scores for the global best of the viewed challenge. Rendered
  // on demand into the solution panel's `.solution-score` block when the user
  // clicks the score; nothing is shown in the stats bar itself.
  private trackScores: Record<string, number> | null = null;
  // Track the actual DOM element we bound the click to — when the user
  // switches challenges, main.ts rebuilds the display panel and replaces
  // the `.solution-score-value` element. Comparing by reference catches
  // that and lets us rebind to the new node.
  private boundScoreEl: HTMLElement | null = null;
  private apiUrl = "";
  private viewedChallenge = "";

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="stats-bar">
        <div class="stats-logo">
          <svg class="stats-mark" viewBox="0 0 64 64" aria-hidden="true">
            <defs>
              <linearGradient id="statsFlameG" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#FFD74A"/>
                <stop offset="40%" stop-color="#FF8A2B"/>
                <stop offset="80%" stop-color="#D24515"/>
                <stop offset="100%" stop-color="#7A2A0F"/>
              </linearGradient>
              <radialGradient id="statsCoreG" cx="50%" cy="60%" r="55%">
                <stop offset="0%" stop-color="#FFFFFF"/>
                <stop offset="55%" stop-color="#FFE8A8" stop-opacity="0.85"/>
                <stop offset="100%" stop-color="#FFE8A8" stop-opacity="0"/>
              </radialGradient>
            </defs>
            <path d="M32 2 C 28 10, 22 14, 20 22 C 18 30, 21 38, 26 42 C 23 38, 23 33, 27 31 C 25 36, 28 41, 32 41 C 30 36, 31 31, 33 27 C 35 31, 36 36, 38 41 C 40 38, 39 33, 37 31 C 41 33, 43 38, 40 42 C 44 38, 47 30, 44 22 C 42 14, 36 10, 32 2 Z" fill="url(#statsFlameG)"/>
            <ellipse cx="32" cy="32" rx="6" ry="9" fill="url(#statsCoreG)"/>
            <g fill="#FFF6D6" opacity="0.95">
              <circle cx="28" cy="18" r="1.1"/>
              <circle cx="36" cy="22" r="1.1"/>
              <circle cx="26" cy="28" r="1"/>
              <circle cx="38" cy="30" r="1"/>
              <circle cx="32" cy="36" r="1"/>
            </g>
            <g stroke="#FFF6D6" stroke-width="0.7" opacity="0.7" fill="none">
              <line x1="28" y1="18" x2="36" y2="22"/>
              <line x1="28" y1="18" x2="26" y2="28"/>
              <line x1="36" y1="22" x2="38" y2="30"/>
              <line x1="26" y1="28" x2="32" y2="36"/>
              <line x1="38" y1="30" x2="32" y2="36"/>
            </g>
            <path d="M21 41 L 43 41 L 40 46 L 24 46 Z" fill="#241914"/>
            <rect x="22" y="43" width="20" height="1.2" fill="#5A3A1F" opacity="0.6"/>
            <g fill="#8A6230">
              <rect x="24" y="44.6" width="1.5" height="1.3"/>
              <rect x="27" y="44.6" width="1.5" height="1.3"/>
              <rect x="30" y="44.6" width="1.5" height="1.3"/>
              <rect x="33" y="44.6" width="1.5" height="1.3"/>
              <rect x="36" y="44.6" width="1.5" height="1.3"/>
              <rect x="39" y="44.6" width="1.5" height="1.3"/>
            </g>
            <rect x="28" y="46" width="8" height="9" fill="#241914"/>
            <rect x="28" y="46" width="2" height="9" fill="#3F2918"/>
            <path d="M21 51 C 17 51, 15 54, 16 58 C 16 60, 18 62, 21 62 L 32 62 C 33 62, 34 61, 34 60 L 34 50 C 34 49, 33 48, 32 48 Z" fill="#241914"/>
            <path d="M32 48 C 35 48, 37 49, 37 52 C 37 54, 35 55, 33 55 L 32 55 Z" fill="#241914"/>
            <line x1="21" y1="53" x2="30" y2="53" stroke="#3F2918" stroke-width="0.7" opacity="0.7"/>
            <line x1="21" y1="56" x2="30" y2="56" stroke="#3F2918" stroke-width="0.7" opacity="0.7"/>
            <line x1="21" y1="59" x2="30" y2="59" stroke="#3F2918" stroke-width="0.7" opacity="0.7"/>
            <path d="M2 64 L 16 56 L 21 60 L 8 64 Z" fill="#241914"/>
          </svg>
          <span class="stats-wordmark">
            <span class="stats-title">Prometheus</span>
            <span class="stats-subtitle">AI Driven Discovery of Algorithms</span>
          </span>
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

    this.apiUrl = resolveApiUrl();
    this.viewedChallenge = resolveViewedChallenge();
    this.attachScoreClick();

    // The user can switch challenges from the dropdown. When they do,
    // main.ts rebuilds the display panel — wiping the `.solution-score`
    // DOM that our click handler was bound to. Reset `clickAttached` so
    // attachScoreClick re-binds to the freshly-rendered score element on
    // the new challenge's panel, and re-hydrate from /api/state since
    // best_track_scores changes shape entirely (different track keys).
    // main.ts's onViewedChallengeChange listener rebuilds the display
    // panel and then broadcasts a `reset` event to every panel — which
    // is where we re-bind the click handler and re-hydrate (see the
    // reset branch in handleMessage). All we need to do here is keep
    // `viewedChallenge` current so the hydrate URL uses the new value.
    onViewedChallengeChange((ch) => {
      this.viewedChallenge = ch;
    });
  }

  // Wire a click handler on the active solution panel's `.solution-score-value`
  // so clicking the big number toggles the track-score breakdown. The
  // display panel is reconstructed when the user switches challenges,
  // so we compare by element reference and rebind whenever the score
  // node changes. Solution panels initialise after stats, so retry
  // briefly until the element exists.
  private attachScoreClick(retries = 20) {
    const scoreParent = document.querySelector(".solution-score") as HTMLElement | null;
    const scoreVal = scoreParent?.querySelector(".solution-score-value") as HTMLElement | null;
    if (!scoreParent || !scoreVal) {
      if (retries > 0) setTimeout(() => this.attachScoreClick(retries - 1), 100);
      return;
    }
    if (this.boundScoreEl === scoreVal) {
      // Same element we already wired up — no-op.
      this.renderTrackBreakdown();
      if (!this.trackScores) void this.hydrateFromState();
      return;
    }
    this.boundScoreEl = scoreVal;
    scoreVal.style.cursor = "pointer";
    scoreVal.title = "Click to show per-track scores";
    scoreVal.addEventListener("click", () => {
      const expanding = !scoreParent.classList.contains("solution-score--expanded");
      scoreParent.classList.toggle("solution-score--expanded");
      // Resilience: if we're opening the popover and we have no track_scores
      // (e.g. dashboard was opened mid-iteration and missed the new_global_best
      // WS event), pull best_track_scores from /api/state so the popover
      // populates instead of being an empty box.
      if (expanding && (!this.trackScores || Object.keys(this.trackScores).length === 0)) {
        void this.hydrateFromState();
      }
    });
    this.renderTrackBreakdown();
    // Best-effort hydrate at init so the first click already has data.
    if (!this.trackScores) void this.hydrateFromState();
  }

  // Pull best_track_scores from /api/state for the viewed challenge. Used as
  // a fallback when the WS new_global_best event is missed (dashboard opened
  // after the publish, brief disconnect, or early-load race).
  private async hydrateFromState() {
    if (!this.apiUrl) return;
    // Read the viewed challenge directly rather than from this.viewedChallenge.
    // main.ts dispatches `reset` to all panels BEFORE our own
    // onViewedChallengeChange listener has fired, so this.viewedChallenge can
    // be one tick stale during the reset handler — which would request stats
    // for the *previous* challenge and re-populate the popover with its
    // track scores after the reset cleared them.
    const requested = getViewedChallenge();
    try {
      const url = `${this.apiUrl}/api/state?challenge=${encodeURIComponent(requested)}`;
      const res = await fetch(url);
      if (!res.ok) return;
      // Discard stale responses: if the user has already switched again
      // before this resolved, don't overwrite trackScores with the
      // previous challenge's data.
      if (requested !== getViewedChallenge()) return;
      const state = await res.json();
      const ts = state?.best_track_scores;
      if (ts && typeof ts === "object" && Object.keys(ts).length > 0) {
        this.trackScores = ts as Record<string, number>;
      } else {
        // Viewed challenge has no published best yet — make sure we
        // don't render the previous challenge's chips.
        this.trackScores = null;
      }
      this.renderTrackBreakdown();
    } catch {
      // Swallow — popover will just stay empty and a future new_global_best
      // event will repopulate.
    }
  }

  // Per-track breakdown of the swarm's best program. Only shown for the
  // global best — agents' individual track-by-track scores aren't surfaced.
  // Injected into the solution panel's `.solution-score` block as a popover
  // that stays hidden until the user clicks the score.
  private renderTrackBreakdown() {
    const scoreParent = document.querySelector(".solution-score") as HTMLElement | null;
    if (!scoreParent) return;
    let host = scoreParent.querySelector(".track-breakdown") as HTMLElement | null;
    if (!this.trackScores || Object.keys(this.trackScores).length === 0) {
      if (host) host.innerHTML = "";
      scoreParent.classList.remove("solution-score--expanded");
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
      // main.ts dispatches `reset` to every panel AFTER it has rebuilt
      // the display panel via constructDisplayPanel(). At that point the
      // .solution-score DOM is fresh, so we can rebind to the new node
      // (boundScoreEl comparison inside attachScoreClick is a no-op when
      // it's the same node as before — i.e. for admin resets that don't
      // rebuild the display panel).
      this.attachScoreClick();
      void this.hydrateFromState();
      return;
    }

    if (msg.type === "stats_update") {
      // ACTIVE = agents currently working on the viewed challenge (not the
      // global swarm size). main.ts slices the per_challenge map so
      // `msg.active_agents` is per-challenge after the filter. The
      // flattened fields are optional in the schema (the wire form only
      // carries `per_challenge`); default to 0 here.
      counterTween(this.agentsEl, msg.active_agents ?? 0);
      counterTween(this.experimentsEl, msg.total_experiments ?? 0);

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
