import type { Panel, WSMessage } from "../types";
import { counterTween, pulseGlow } from "../lib/animate";
import { formatScore } from "../lib/format";
import { getViewedChallenge, onViewedChallengeChange } from "../lib/viewedChallenge";
import { getSwarmType, onSwarmConfigChange } from "../lib/swarmConfig";


// The clickable score lives in different DOM per challenge: CPU panels render
// `.solution-score-value` inside `.solution-score`; GPU panels (hypergraph,
// neuralnet_optimizer, vector_search) render it in their own stat bar and opt
// in with `data-track-score`. We match either and treat the value's parent as
// the popover container (toggling `solution-score--expanded` + hosting the
// `.track-breakdown`), so the per-track popover is panel-layout-agnostic.
const SCORE_VALUE_SELECTOR = ".solution-score-value, [data-track-score]";


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
  private agentsTotalEl!: HTMLElement;
  private trajectoriesEl!: HTMLElement;
  // Latest track_scores for the global best of the viewed challenge. Rendered
  // on demand into the solution panel's `.solution-score` block when the user
  // clicks the score; nothing is shown in the stats bar itself.
  private trackScores: Record<string, number> | null = null;
  // Track the actual DOM element we bound the click to — when the user
  // switches challenges, main.ts rebuilds the display panel and replaces
  // the `.solution-score-value` element. Comparing by reference catches
  // that and lets us rebind to the new node.
  private boundScoreEl: HTMLElement | null = null;
  // Single pending retry timer for attachScoreClick. attachScoreClick is
  // called from init, reset, and new_global_best — without this, three
  // concurrent retry chains can stack 60+ setTimeouts before the score
  // element appears.
  private scoreRetryHandle: ReturnType<typeof setTimeout> | null = null;
  private apiUrl = "";
  private viewedChallenge = "";

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="stats-bar">
        <div class="stats-logo">
          <img class="stats-mark" src="/prometheus-icon.png" alt="" draggable="false" />

          <span class="stats-wordmark">
            <span class="stats-title">Prometheus</span>
            <span class="stats-subtitle">AI Driven Discovery of Algorithms</span>
          </span>
          <span id="ws-status" class="ws-status connected">LIVE</span>
          <span id="swarm-type-badge" class="swarm-type-badge"></span>
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
            <span class="stat-label">RUNS</span>
            <span class="stat-value" id="stat-experiments-val">0</span>
          </div>
          <div class="stat-chip" id="stat-agents-total">
            <span class="stat-label">AGENTS</span>
            <span class="stat-value" id="stat-agents-total-val">0</span>
          </div>
          <div class="stat-chip" id="stat-trajectories">
            <span class="stat-label">TRAJECTORIES</span>
            <span class="stat-value" id="stat-trajectories-val">0</span>
          </div>
        </div>
      </div>
    `;

    this.agentsEl = document.getElementById("stat-agents-val")!;
    this.experimentsEl = document.getElementById("stat-experiments-val")!;
    this.agentsTotalEl = document.getElementById("stat-agents-total-val")!;
    this.trajectoriesEl = document.getElementById("stat-trajectories-val")!;

    this.updateSwarmTypeBadge();
    onSwarmConfigChange(() => this.updateSwarmTypeBadge());

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
    // Cancel any prior retry chain. Without this, init/reset/new_global_best
    // each spawn an independent setTimeout chain that races to find the same
    // element. They eventually short-circuit via boundScoreEl, but only
    // after wasting up to 20×100ms of ticks each.
    if (this.scoreRetryHandle !== null) {
      clearTimeout(this.scoreRetryHandle);
      this.scoreRetryHandle = null;
    }
    const scoreVal = document.querySelector(SCORE_VALUE_SELECTOR) as HTMLElement | null;
    const scoreParent = scoreVal?.parentElement as HTMLElement | null;
    if (!scoreParent || !scoreVal) {
      if (retries > 0) {
        this.scoreRetryHandle = setTimeout(() => {
          this.scoreRetryHandle = null;
          this.attachScoreClick(retries - 1);
        }, 100);
      }
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
    const scoreVal = document.querySelector(SCORE_VALUE_SELECTOR) as HTMLElement | null;
    const scoreParent = scoreVal?.parentElement as HTMLElement | null;
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

  private updateSwarmTypeBadge() {
    const el = document.getElementById("swarm-type-badge");
    if (!el) return;
    const t = getSwarmType();
    el.textContent = t.toUpperCase();
    el.className = `swarm-type-badge swarm-type-${t}`;
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.agentsEl.textContent = "0";
      this.experimentsEl.textContent = "0";
      this.agentsTotalEl.textContent = "0";
      this.trajectoriesEl.textContent = "0";
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
      counterTween(this.agentsTotalEl, msg.total_agents_in_challenge ?? 0);
      counterTween(this.trajectoriesEl, msg.total_trajectories ?? 0);
    }

    if (msg.type === "agent_joined") {
      pulseGlow(document.getElementById("stat-agents")!);
    }

    if (msg.type === "experiment_published") {
      pulseGlow(document.getElementById("stat-experiments")!);
    }

    if (msg.type === "new_global_best") {
      this.trackScores = msg.track_scores ?? null;
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
