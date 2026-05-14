import "./style.css";
import { SwarmWebSocket } from "./lib/websocket";
import { getDashboardUrls, installKeyboardNav } from "./lib/bootstrap";
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
import { buildPanelFor, isKnownChallenge } from "./challenges/registry";

import type { WSMessage, Panel } from "./types";

const { wsUrl, apiUrl } = getDashboardUrls();

// ── Initialize panels ──
// Panels are constructed inside `bootstrap()` after loadSwarmConfig() so
// that init() sees the active challenge on first paint. The display panel
// (the per-challenge visualization) is rebuilt when the user picks a
// different challenge in the selector; everything else stays mounted and
// re-fetches its data via setChallenge().
const panels: Panel[] = [];
let chartPanel: ChartPanel;
let feedPanel: FeedPanel;
let displayPanel: Panel | undefined;

// agent_id → current agent_name. Authoritative source on the dashboard
// side; populated from every LeaderboardUpdate (which on the server JOINs
// agents.name live) and updated in-place on agent_renamed. Read by the
// feed panel so it can render names from the agent_id rather than relying
// on whatever snapshot was attached to a given event.
const agentNameMap = new Map<string, string>();
const lookupAgentName = (agent_id: string): string | undefined =>
  agentNameMap.get(agent_id);

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
  // The registry maps challenge id → panel factory. Adding a new challenge
  // is one entry in challenges/registry.ts — no edit here.
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
  feedPanel = initPanel(FeedPanel, "panel-feed");
  feedPanel.setNameLookup(lookupAgentName);
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

function handleMessage(msg: WSMessage) {
  // Drop challenge-scoped events that don't belong to the viewed challenge.
  // `agent_joined`, `swarm_config_updated`, `admin_broadcast`, and the
  // global slice of `stats_update` don't get filtered.
  const m = msg as any;
  if (CHALLENGE_SCOPED[m.type] && m.challenge && m.challenge !== getViewedChallenge()) {
    return;
  }

  // Keep agent_id → name map in sync. Leaderboard entries are server-JOINed
  // so they always carry the current name; agent_renamed is the explicit
  // signal when no leaderboard update follows. agent_joined seeds first-name
  // entries before any leaderboard fires.
  if (msg.type === "leaderboard_update") {
    for (const entry of msg.entries) {
      if (entry.agent_id && entry.agent_name) {
        agentNameMap.set(entry.agent_id, entry.agent_name);
      }
    }
  } else if (msg.type === "agent_renamed") {
    agentNameMap.set(msg.agent_id, msg.new_name);
  } else if (msg.type === "agent_joined") {
    agentNameMap.set(msg.agent_id, msg.agent_name);
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
  handleSwarmConfigEvent(apiUrl, msg);

  panels.forEach((panel) => panel.handleMessage(msg));

  // Per-challenge admin reset: panels have already cleared their state
  // above. Re-hydrate from /api/state + /api/replay so counters and
  // baselines are correct without requiring a full page reload. Without
  // this, panels sit blank until the next live event arrives.
  if (msg.type === "reset" && msg.challenge === getViewedChallenge()) {
    void loadInitialState(apiUrl, getViewedChallenge());
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

    handleMessage({
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
      handleMessage({
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
      handleMessage({
        type: "leaderboard_update",
        entries: state.leaderboard,
        timestamp: new Date().toISOString(),
      } as any);
    }

    // Merge experiments + hypotheses into a single chronologically-sorted
    // stream and dispatch oldest-first. feed.ts prepends each event, so
    // the last-dispatched lands at the top — meaning the newest event
    // (across both kinds) ends up at the top of the feed regardless of
    // which list it came from. Sort uses full ISO timestamps including
    // date, so cross-day events order correctly.
    type FeedSeed = { kind: "experiment"; ts: string; data: any }
                  | { kind: "hypothesis"; ts: string; data: any };
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
    seeds.sort((a, b) => a.ts.localeCompare(b.ts));
    for (const seed of seeds) {
      if (seed.kind === "experiment") {
        const exp = seed.data;
        handleMessage({
          type: "experiment_published",
          experiment_id: exp.id || "",
          agent_name: exp.agent_name,
          // Server now includes agent_id in /api/state's recent_experiments
          // (was missing before). getAgentColor is keyed on agent_id, so
          // threading the real id through makes backfilled experiments
          // render with the same palette color as live WS events.
          agent_id: exp.agent_id || "",
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
      } else {
        const hyp = seed.data;
        handleMessage({
          type: "hypothesis_proposed",
          hypothesis_id: hyp.id || "",
          agent_name: hyp.agent_name,
          // Server's recent_hypotheses already includes agent_id; thread it
          // through so the feed dot uses the agent's palette color.
          agent_id: hyp.agent_id || "",
          title: hyp.title,
          description: hyp.description || "",
          strategy_tag: hyp.strategy_tag,
          parent_hypothesis_id: hyp.parent_hypothesis_id || null,
          // Original created_at preserves timestamps across challenge
          // switches; otherwise everything would re-stamp to "just now".
          timestamp: seed.ts,
        } as any);
      }
    }

    // Backfill the live feed from /api/messages so chat history AND
    // agent_joined events survive a page reload. Server returns rows for
    // the viewed challenge plus all msg_type='agent_joined' rows regardless
    // of challenge (joins are swarm-wide). Dispatched oldest-first so the
    // newest entries land at the top of the feed alongside synthesised
    // experiment/hypothesis events above.
    void fetch(`${apiUrl}/api/messages?limit=200&challenge=${encodeURIComponent(challenge)}`)
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: Array<{
        id: string;
        agent_id: string | null;
        agent_name: string;
        content: string;
        msg_type: string;
        created_at: string;
      }>) => {
        if (challenge !== getViewedChallenge()) return;
        rows.sort((a, b) => a.created_at.localeCompare(b.created_at));
        for (const row of rows) {
          if (row.msg_type === "agent_joined") {
            handleMessage({
              type: "agent_joined",
              agent_id: row.agent_id || "",
              agent_name: row.agent_name,
              timestamp: row.created_at,
            } as any);
          } else {
            handleMessage({
              type: "chat_message",
              message_id: row.id,
              agent_id: row.agent_id,
              agent_name: row.agent_name,
              content: row.content,
              msg_type: row.msg_type === "milestone" ? "milestone" : "agent",
              timestamp: row.created_at,
            } as any);
          }
        }
      })
      .catch((e) => console.warn("[Dashboard] /api/messages backfill failed:", e));

    soundEnabled = true;
    console.log("[Dashboard] Loaded initial state for challenge:", challenge);

    // Seed the chart's score-history line when the (compact) replay
    // lands. Doesn't block visualization or stats panels above.
    // Drop the result if the user has switched challenges while the
    // replay was in flight — otherwise stale data overwrites the chart.
    replayPromise.then((replay) => {
      if (challenge !== getViewedChallenge()) return;
      if (replay && replay.length) chartPanel.seedHistory(replay);
    });
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
  void loadInitialState(apiUrl, c);
});

// ── Welcome overlay ──
initWelcome();

// ── Keyboard navigation ──
installKeyboardNav("main");
document.addEventListener("keydown", (e) => {
  if (e.key === "j" || e.key === "J") toggleWelcome();
  if (e.key === "r" || e.key === "R") startReplay(apiUrl, handleMessage);
});

// ── Connect ──
console.log(`[Dashboard] Connecting to ${wsUrl}, API: ${apiUrl}`);

void loadSwarmConfig(apiUrl).then(() => {
  constructPanels();
  void loadInitialState(apiUrl, getViewedChallenge());
});

const ws = new SwarmWebSocket(wsUrl);
ws.onMessage(handleMessage);
ws.connect();
