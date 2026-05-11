import "@phosphor-icons/web/regular/style.css";
import "./style.css";
import { SwarmWebSocket } from "./lib/websocket";
import { MockDataGenerator } from "./mock";
import { viewportFlash } from "./lib/animate";
import {
  soundAgentJoined, soundHypothesisProposed, soundExperimentPublished,
  soundNewGlobalBest, startHeartbeat,
} from "./lib/sounds";
import { initWelcome, toggleWelcome } from "./lib/welcome";
import { startReplay } from "./lib/replay";
import {
  loadSwarmConfig,
  handleWsEvent as handleSwarmConfigEvent,
} from "./lib/swarmConfig";
import {
  getViewedChallenge,
  onViewedChallengeChange,
} from "./lib/viewedChallenge";

import { ChallengeSelectorPanel } from "./panels/challenge-selector";
import { StatsPanel } from "./panels/stats";
import { ChartPanel } from "./panels/chart";
import { DiversityPanel } from "./panels/diversity";
import { FeedPanel } from "./panels/feed";
import { LeaderboardPanel } from "./panels/leaderboard";
import { buildPanelFor, isKnownChallenge } from "./lib/challengeRegistry";

import type { WSMessage, Panel } from "./types";

// ── Config ──
const params = new URLSearchParams(window.location.search);
const isMock = params.has("mock");
const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
const wsUrl = params.get("ws") || `${wsProtocol}//${window.location.host}/ws/dashboard`;

// Derive REST API URL from WS URL
function getApiUrl(): string {
  const explicit = params.get("api");
  if (explicit) return explicit;
  return wsUrl
    .replace("ws://", "http://")
    .replace("wss://", "https://")
    .replace("/ws/dashboard", "");
}

// ── Initialize panels ──
// Panels are constructed inside `bootstrap()` after loadSwarmConfig() so
// that init() sees the active challenge on first paint. The display panel
// (the per-challenge visualization) is rebuilt when the user picks a
// different challenge in the selector; everything else stays mounted and
// re-fetches its data via setChallenge().
const panels: Panel[] = [];
let chartPanel: ChartPanel;
let displayPanel: Panel | undefined;

function initPanel<T extends Panel>(PanelClass: new () => T, containerId: string): T {
  const panel = new PanelClass();
  const container = document.getElementById(containerId)!;
  panel.init(container);
  panels.push(panel);
  return panel;
}

function constructDisplayPanel() {
  const container = document.getElementById("panel-display");
  if (!container) return;
  if (displayPanel) {
    displayPanel.dispose?.();
    panels.splice(panels.indexOf(displayPanel), 1);
    displayPanel = undefined;
    container.innerHTML = "";
  }
  // The registry maps challenge id → panel factory. Adding a 6th challenge
  // is one entry in lib/challengeRegistry.ts — no edit here.
  const challenge = getViewedChallenge();
  if (!isKnownChallenge(challenge)) {
    console.warn(`[Dashboard] No panel registered for challenge "${challenge}"`);
    return;
  }
  displayPanel = buildPanelFor(challenge);
  displayPanel.init(container);
  panels.push(displayPanel);
}

function constructPanels() {
  initPanel(ChallengeSelectorPanel, "panel-challenge-selector");
  initPanel(StatsPanel, "panel-stats");
  constructDisplayPanel();
  chartPanel = initPanel(ChartPanel, "panel-chart");
  initPanel(DiversityPanel, "panel-diversity");
  initPanel(FeedPanel, "panel-feed");
  initPanel(LeaderboardPanel, "panel-leaderboard");
}

// ── Message dispatch ──
let soundEnabled = false;

// Events that carry per-challenge data; drop them when their `challenge`
// doesn't match the user's viewed challenge so panels render consistent state.
const CHALLENGE_SCOPED: Record<string, true> = {
  experiment_published: true,
  hypothesis_proposed: true,
  new_global_best: true,
  leaderboard_update: true,
  chat_message: true,
  trajectory_reset: true,
  hypothesis_status_changed: true,
  // /api/admin/reset_challenge broadcasts `{type:"reset", challenge:"…"}`.
  // Without this entry, a reset on knapsack would clear panels viewing
  // job_scheduling. The filter below drops any scoped event whose
  // `challenge` field doesn't match the viewed challenge.
  reset: true,
};

// Live-event buffer. Starts as [] (= buffering) so any WS message arriving
// before constructPanels() runs is queued instead of being dropped against
// an empty `panels` array. When set to null, messages flow straight through.
// We re-enter buffering during challenge switches and per-challenge admin
// resets so historical seeds in loadInitialState always land on top of any
// concurrent live events, preserving chronological feed order.
let liveBuffer: WSMessage[] | null = [];

function beginBuffering(): void {
  if (liveBuffer === null) liveBuffer = [];
}

function flushBuffer(): void {
  if (liveBuffer === null) return;
  const queued = liveBuffer;
  liveBuffer = null;
  // Dispatch oldest-first so the newest live event ends up on top of the
  // feed (which prepends). This matches the order loadInitialState used
  // for its historical seeds.
  queued.sort((a, b) => {
    const at = (a as any).timestamp ?? "";
    const bt = (b as any).timestamp ?? "";
    return String(at).localeCompare(String(bt));
  });
  for (const m of queued) dispatchMessage(m);
}

function handleMessage(msg: WSMessage) {
  if (liveBuffer !== null) {
    liveBuffer.push(msg);
    return;
  }
  dispatchMessage(msg);
}

function dispatchMessage(msg: WSMessage) {
  // Drop challenge-scoped events that don't belong to the viewed challenge.
  // `agent_joined`, `swarm_config_updated`, `admin_broadcast`, and the
  // global slice of `stats_update` don't get filtered.
  const m = msg as any;
  if (CHALLENGE_SCOPED[m.type] && m.challenge && m.challenge !== getViewedChallenge()) {
    return;
  }

  if (soundEnabled) {
    if (msg.type === "agent_joined") soundAgentJoined();
    if (msg.type === "hypothesis_proposed") soundHypothesisProposed(msg.strategy_tag);
    if (msg.type === "experiment_published") soundExperimentPublished();
    if (msg.type === "new_global_best") soundNewGlobalBest();
    if (msg.type === "stats_update") startHeartbeat(msg.total_agents ?? msg.active_agents ?? 0);
  }

  if (msg.type === "new_global_best") {
    viewportFlash("rgba(184, 84, 31, 0.06)", 150);
  }

  // For stats_update we slice `per_challenge` down to the viewed
  // challenge so panels see the right counters. `per_challenge` is the
  // sole source of truth on the wire.
  if (msg.type === "stats_update" && (msg as any).per_challenge) {
    const sliced = (msg as any).per_challenge[getViewedChallenge()] ?? {};
    msg = {
      ...msg,
      active_agents: sliced.active_agents ?? 0,
      total_experiments: sliced.total_experiments ?? 0,
      total_agents_in_challenge: sliced.total_agents_in_challenge ?? 0,
      total_trajectories: sliced.total_trajectories ?? 0,
      hypotheses_count: sliced.hypotheses_count ?? 0,
      best_score: sliced.best_score ?? null,
      baseline_score: sliced.baseline_score ?? null,
      num_instances: sliced.num_instances ?? 0,
      improvement_pct: sliced.improvement_pct ?? 0,
    } as any;
  }

  // Refetch swarm config when the host switches the active challenge.
  handleSwarmConfigEvent(getApiUrl(), msg);

  panels.forEach((panel) => panel.handleMessage(msg));

  // Per-challenge admin reset: panels have already cleared their state
  // above. Re-hydrate from /api/state + /api/replay so counters and
  // baselines are correct without requiring a full page reload. Without
  // this, panels sit blank until the next live event arrives. Buffer
  // concurrent live events until the seed completes so they don't get
  // overwritten by the historical replay.
  if (msg.type === "reset" && msg.challenge === getViewedChallenge()) {
    beginBuffering();
    void loadInitialState(getApiUrl(), getViewedChallenge()).then(flushBuffer);
  }
}

// ── Fetch initial state from REST API ──
async function loadInitialState(apiUrl: string, challenge: string) {
  try {
    const q = `?challenge=${encodeURIComponent(challenge)}`;
    // Two independent fetches:
    //  - /api/state is small (≤ 350 KB) and unblocks the visualization +
    //    stats panels via a synthesized new_global_best.
    //  - /api/replay is large (up to ~1 MB on heavily-worked challenges)
    //    and only feeds the chart's score-history line. We use
    //    `compact=1` to omit each row's solution_data field — the chart
    //    only needs score/agent/timestamp.
    // Run them in parallel but DO NOT block dispatch on the replay; the
    // chart hydrates after state without making the user wait.
    const stateRes = await fetch(`${apiUrl}/api/state${q}`);
    if (!stateRes.ok) return;
    const state = await stateRes.json();
    const hypothesisCount =
      state.hypotheses_count ?? (state.recent_hypotheses?.length || 0);

    // Kick off the replay fetch in parallel; we'll seed the chart when
    // it lands. Other panels don't depend on it.
    const replayPromise: Promise<Array<{
      experiment_id: string;
      agent_name: string;
      agent_id?: string;
      score: number;
      created_at: string;
    }>> = fetch(`${apiUrl}/api/replay${q}&compact=1`)
      .then((r) => (r.ok ? r.json() : []))
      .catch(() => []);

    dispatchMessage({
      type: "stats_update",
      active_agents: state.active_agents,
      total_agents: state.total_agents ?? state.active_agents,
      total_agents_in_challenge: state.total_agents_in_challenge ?? 0,
      total_trajectories: state.total_trajectories ?? 0,
      total_experiments: state.total_experiments ?? state.recent_experiments?.length ?? 0,
      hypotheses_count: hypothesisCount,
      best_score: state.best_score,
      baseline_score: state.baseline_score,
      num_instances: state.num_instances || 1,
      improvement_pct: state.improvement_pct || 0,
      timestamp: new Date().toISOString(),
    } as any);

    // Synthesize a new_global_best so panels (display visualization, stats
    // hero, track breakdown) hydrate from /api/state on first paint. Fire
    // when EITHER solution_data OR track_scores are present — track_scores
    // alone is enough to render the breakdown chips.
    if (
      state.best_score != null &&
      (state.best_solution_data || state.best_track_scores)
    ) {
      // Use what we know from /api/state for agent name/id; the replay
      // result (if it lands) doesn't change this.
      const recentBest = (state.recent_experiments || []).find((e: any) => e.is_new_best);
      dispatchMessage({
        type: "new_global_best",
        experiment_id: state.best_experiment_id || "",
        agent_name: recentBest?.agent_name || "swarm",
        agent_id: recentBest?.agent_id || "",
        score: state.best_score,
        improvement_pct: state.improvement_pct || 0,
        incremental_improvement_pct: null,
        num_instances: state.num_instances || 1,
        solution_data: state.best_solution_data,
        track_scores: state.best_track_scores ?? null,
        timestamp: new Date().toISOString(),
      } as any);
    }

    if (state.leaderboard?.length) {
      dispatchMessage({
        type: "leaderboard_update",
        entries: state.leaderboard,
        timestamp: new Date().toISOString(),
      } as any);
    }

    // Merge experiments + hypotheses + agent registrations into a single
    // chronologically-sorted stream and dispatch oldest-first. feed.ts
    // prepends each event, so the last-dispatched lands at the top —
    // meaning the newest event (across all kinds) ends up at the top of
    // the feed regardless of which list it came from. Sort uses full ISO
    // timestamps including date, so cross-day events order correctly.
    // `agent_joined` is global (not challenge-scoped) and is replayed
    // here so panel resets don't permanently lose "X joined the swarm"
    // lines — WS delivery is otherwise the only path that surfaces them.
    type FeedSeed =
      | { kind: "experiment"; ts: string; data: any }
      | { kind: "hypothesis"; ts: string; data: any }
      | { kind: "agent"; ts: string; data: any };
    const seeds: FeedSeed[] = [];
    for (const exp of (state.recent_experiments || [])) {
      seeds.push({
        kind: "experiment",
        ts: exp.created_at || new Date().toISOString(),
        data: exp,
      });
    }
    for (const hyp of (state.recent_hypotheses || [])) {
      seeds.push({
        kind: "hypothesis",
        ts: hyp.created_at || new Date().toISOString(),
        data: hyp,
      });
    }
    for (const ag of (state.recent_agents || [])) {
      seeds.push({
        kind: "agent",
        ts: ag.registered_at || new Date().toISOString(),
        data: ag,
      });
    }
    seeds.sort((a, b) => a.ts.localeCompare(b.ts));
    for (const seed of seeds) {
      if (seed.kind === "experiment") {
        const exp = seed.data;
        dispatchMessage({
          type: "experiment_published",
          experiment_id: exp.id || "",
          agent_name: exp.agent_name,
          agent_id: "",
          score: exp.score,
          feasible: exp.feasible !== false,
          improvement_pct: exp.improvement_pct || 0,
          delta_vs_best_pct: exp.delta_vs_best_pct ?? null,
          delta_vs_own_best_pct: exp.delta_vs_own_best_pct ?? null,
          beats_own_best: exp.beats_own_best === true,
          num_instances: state.num_instances || 1,
          is_new_best: exp.is_new_best === true,
          hypothesis_id: null,
          notes: exp.notes || "",
          timestamp: seed.ts,
        } as any);
      } else if (seed.kind === "hypothesis") {
        const hyp = seed.data;
        dispatchMessage({
          type: "hypothesis_proposed",
          hypothesis_id: hyp.id || "",
          agent_name: hyp.agent_name,
          agent_id: "",
          title: hyp.title,
          description: hyp.description || "",
          strategy_tag: hyp.strategy_tag,
          parent_hypothesis_id: hyp.parent_hypothesis_id || null,
          // Original created_at preserves timestamps across challenge
          // switches; otherwise everything would re-stamp to "just now".
          timestamp: seed.ts,
        } as any);
      } else {
        const ag = seed.data;
        dispatchMessage({
          type: "agent_joined",
          agent_id: ag.id,
          agent_name: ag.name,
          timestamp: seed.ts,
        } as any);
      }
    }

    soundEnabled = true;
    console.log("[Dashboard] Loaded initial state for challenge:", challenge);

    // Seed the chart's score-history line when the (compact) replay
    // lands. Doesn't block visualization or stats panels above.
    replayPromise.then((replay) => {
      if (replay && replay.length) chartPanel.seedHistory(replay);
    });
  } catch (e) {
    console.warn("[Dashboard] Failed to load initial state:", e);
  }
}

// React to user picking a different challenge in the selector. Reconstruct
// the display panel (its component class differs per challenge), clear
// challenge-scoped state on every other panel via the existing `reset`
// handler, then re-hydrate from REST with `?challenge=`. Buffer live WS
// events during the re-hydrate so they can't beat the historical seeds
// to the panels and get visually overwritten.
onViewedChallengeChange((c) => {
  beginBuffering();
  constructDisplayPanel();
  // Use the `reset` event the panels already handle to clear their
  // challenge-scoped state (chart history, leaderboard rows, feed items).
  panels.forEach((p) => {
    p.handleMessage({ type: "reset", timestamp: new Date().toISOString() } as any);
    p.setChallenge?.(c);
  });
  void loadInitialState(getApiUrl(), c).then(flushBuffer);
});

// ── Welcome overlay ──
initWelcome();

// ── Keyboard navigation ──
document.addEventListener("keydown", (e) => {
  if (e.key === "2") window.location.href = "/ideas.html";
  if (e.key === "3") window.location.href = "/diversity.html";
  if (e.key === "4") window.location.href = "/benchmark.html";
  if (e.key === "5") window.location.href = "/trajectories.html";
  if (e.key === "j" || e.key === "J") toggleWelcome();
  if (e.key === "r" || e.key === "R") startReplay(getApiUrl(), dispatchMessage);
});

// ── Connect ──
if (isMock) {
  console.log("[Dashboard] Running in MOCK mode");
  soundEnabled = true;
  constructPanels();
  // No loadInitialState in mock mode — flush the boot-time buffer now
  // so mock-generated events dispatch immediately.
  flushBuffer();
  const mock = new MockDataGenerator();
  mock.onMessage(handleMessage);
  mock.start();

  const wsEl = document.getElementById("ws-status");
  if (wsEl) {
    wsEl.textContent = "MOCK";
    wsEl.className = "ws-status connected";
  }
} else {
  const apiUrl = getApiUrl();
  console.log(`[Dashboard] Connecting to ${wsUrl}, API: ${apiUrl}`);

  // Live events flow into the boot-time buffer (declared at module scope)
  // until constructPanels + loadInitialState finish. Without this, a WS
  // event arriving before constructPanels would dispatch against an empty
  // `panels` array and silently disappear; one arriving mid-load would be
  // visually overrun by historical seeds prepended on top of it.
  void loadSwarmConfig(apiUrl).then(() => {
    constructPanels();
    void loadInitialState(apiUrl, getViewedChallenge()).then(flushBuffer);
  });

  const ws = new SwarmWebSocket(wsUrl);
  ws.onMessage(handleMessage);
  ws.connect();
}
