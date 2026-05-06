import "./style.css";
import { initParticles } from "./lib/particles";
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
import { SolutionPanel } from "./panels/solution";
import { GanttPanel } from "./panels/gantt";
import { KnapsackPanel } from "./panels/knapsack";
import { EnergyPanel } from "./panels/energy";
import { SatPanel } from "./panels/sat";
import { ChartPanel } from "./panels/chart";
import { DiversityPanel } from "./panels/diversity";
import { FeedPanel } from "./panels/feed";
import { LeaderboardPanel } from "./panels/leaderboard";

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

// ── Background particles ──
const canvas = document.getElementById("particleCanvas") as HTMLCanvasElement;
initParticles(canvas);

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
  const challenge = getViewedChallenge();
  if (challenge === "job_scheduling") displayPanel = new GanttPanel();
  else if (challenge === "knapsack") displayPanel = new KnapsackPanel();
  else if (challenge === "energy_arbitrage") displayPanel = new EnergyPanel();
  else if (challenge === "satisfiability") displayPanel = new SatPanel();
  else displayPanel = new SolutionPanel(); // VRP (and any future challenge before it gets a dedicated panel)
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
};

function handleMessage(msg: WSMessage) {
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
    if (msg.type === "stats_update") startHeartbeat(msg.total_agents ?? msg.active_agents);
  }

  if (msg.type === "new_global_best") {
    viewportFlash("rgba(0, 229, 255, 0.03)", 150);
  }

  // For stats_update we want the per-challenge slice for the viewed challenge.
  if (msg.type === "stats_update" && (msg as any).per_challenge) {
    const sliced = (msg as any).per_challenge[getViewedChallenge()] ?? {};
    msg = {
      ...msg,
      active_agents: sliced.active_agents ?? msg.active_agents,
      total_experiments: sliced.total_experiments ?? msg.total_experiments,
      hypotheses_count: sliced.hypotheses_count ?? msg.hypotheses_count,
      best_score: sliced.best_score ?? msg.best_score,
      baseline_score: sliced.baseline_score ?? msg.baseline_score,
      num_instances: sliced.num_instances ?? msg.num_instances,
      improvement_pct: sliced.improvement_pct ?? msg.improvement_pct,
    } as any;
  }

  // Refetch swarm config when the host switches the active challenge.
  handleSwarmConfigEvent(getApiUrl(), msg);

  panels.forEach((panel) => panel.handleMessage(msg));
}

// ── Fetch initial state from REST API ──
async function loadInitialState(apiUrl: string, challenge: string) {
  try {
    const q = `?challenge=${encodeURIComponent(challenge)}`;
    const [stateRes, replayRes] = await Promise.all([
      fetch(`${apiUrl}/api/state${q}`),
      fetch(`${apiUrl}/api/replay${q}`),
    ]);
    if (!stateRes.ok) return;
    const state = await stateRes.json();
    const replay: Array<{
      experiment_id: string;
      agent_name: string;
      agent_id?: string;
      score: number;
      created_at: string;
    }> = replayRes.ok ? await replayRes.json() : [];
    const hypothesisCount =
      state.hypotheses_count ?? (state.recent_hypotheses?.length || 0);

    chartPanel.seedHistory(replay);

    const incrementalPct =
      replay.length >= 2
        ? ((replay[replay.length - 2].score - replay[replay.length - 1].score) /
            replay[replay.length - 2].score) *
          100
        : null;

    handleMessage({
      type: "stats_update",
      active_agents: state.active_agents,
      total_agents: state.total_agents ?? state.active_agents,
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
      handleMessage({
        type: "new_global_best",
        experiment_id: state.best_experiment_id || "",
        agent_name: replay[replay.length - 1]?.agent_name || "swarm",
        agent_id: replay[replay.length - 1]?.agent_id || "",
        score: state.best_score,
        improvement_pct: state.improvement_pct || 0,
        incremental_improvement_pct: incrementalPct,
        num_instances: state.num_instances || 1,
        solution_data: state.best_solution_data,
        track_scores: state.best_track_scores ?? null,
        timestamp: new Date().toISOString(),
      } as any);
    }

    if (state.leaderboard?.length) {
      handleMessage({
        type: "leaderboard_update",
        entries: state.leaderboard,
        timestamp: new Date().toISOString(),
      } as any);
    }

    const recent = (state.recent_experiments || []).slice().reverse();
    for (const exp of recent) {
      handleMessage({
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
        timestamp: exp.created_at || new Date().toISOString(),
      } as any);
    }

    const allHyps = state.recent_hypotheses || [];
    for (const hyp of allHyps) {
      handleMessage({
        type: "hypothesis_proposed",
        hypothesis_id: hyp.id || "",
        agent_name: hyp.agent_name,
        agent_id: "",
        title: hyp.title,
        description: hyp.description || "",
        strategy_tag: hyp.strategy_tag,
        parent_hypothesis_id: hyp.parent_hypothesis_id || null,
        // Use the original `created_at` from the server so timestamps don't
        // get refreshed to "just now" on every challenge switch.
        timestamp: hyp.created_at || new Date().toISOString(),
      } as any);
    }

    soundEnabled = true;
    console.log("[Dashboard] Loaded initial state for challenge:", challenge);
  } catch (e) {
    console.warn("[Dashboard] Failed to load initial state:", e);
  }
}

// React to user picking a different challenge in the selector. Reconstruct
// the display panel (its component class differs per challenge), clear
// challenge-scoped state on every other panel via the existing `reset`
// handler, then re-hydrate from REST with `?challenge=`.
onViewedChallengeChange((c) => {
  constructDisplayPanel();
  // Use the `reset` event the panels already handle to clear their
  // challenge-scoped state (chart history, leaderboard rows, feed items).
  panels.forEach((p) => {
    p.handleMessage({ type: "reset", timestamp: new Date().toISOString() } as any);
    p.setChallenge?.(c);
  });
  void loadInitialState(getApiUrl(), c);
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
  if (e.key === "r" || e.key === "R") startReplay(getApiUrl(), handleMessage);
});

// ── Connect ──
if (isMock) {
  console.log("[Dashboard] Running in MOCK mode");
  soundEnabled = true;
  constructPanels();
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

  void loadSwarmConfig(apiUrl).then(() => {
    constructPanels();
    void loadInitialState(apiUrl, getViewedChallenge());
  });

  const ws = new SwarmWebSocket(wsUrl);
  ws.onMessage(handleMessage);
  ws.connect();
}
