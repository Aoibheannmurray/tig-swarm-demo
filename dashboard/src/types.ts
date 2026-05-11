export interface RoutePoint {
  x: number;
  y: number;
  customer_id: number;
}

export interface VehicleRoute {
  vehicle_id: number;
  path: RoutePoint[];
}

export interface RouteData {
  depot: { x: number; y: number };
  routes: VehicleRoute[];
}

// solution_data from server: dict keyed by instance name
export type AllRouteData = Record<string, RouteData>;

export interface LeaderboardEntry {
  rank: number;
  agent_id: string;
  agent_name: string;
  llm_type?: string;
  runs: number;
  improvements: number;
  runs_since_improvement: number;
  current_score: number | null;
  best_ever_score: number | null;
  num_trajectories: number;
  tacit_knowledge_count: number;
  inspiration_count: number;
  total_tokens: number;
  estimated_cost_usd: number;
  active: boolean;
}

// WebSocket message types — mirror of server/ws_events.py.
//
// `HypothesisProposed` and `HypothesisStatusChanged` are dashboard-only
// synthetic events (main.ts builds them from /api/state's recent_hypotheses
// list); the server itself never emits them over the WebSocket. They stay
// in this union because the dispatch path treats them like any other event.
export type WSMessage =
  | AgentJoined
  | HypothesisProposed
  | HypothesisStatusChanged
  | ExperimentPublished
  | NewGlobalBest
  | LeaderboardUpdate
  | StatsUpdate
  | ChatMessage
  | AdminBroadcastMsg
  | ResetMsg
  | TrajectoryReset
  | SwarmConfigUpdated;

export interface AgentJoined {
  type: "agent_joined";
  agent_id: string;
  agent_name: string;
  timestamp: string;
}

export interface HypothesisProposed {
  type: "hypothesis_proposed";
  hypothesis_id: string;
  agent_name: string;
  agent_id: string;
  title: string;
  description: string;
  strategy_tag: string;
  parent_hypothesis_id: string | null;
  timestamp: string;
}

export interface HypothesisStatusChanged {
  type: "hypothesis_status_changed";
  hypothesis_id: string;
  new_status: string;
  agent_name: string;
  timestamp: string;
}

export interface ExperimentPublished {
  type: "experiment_published";
  // Server-side scope; used by main.ts CHALLENGE_SCOPED filter.
  challenge: string;
  experiment_id: string;
  agent_name: string;
  agent_id: string;
  score: number;
  feasible: boolean;
  improvement_pct: number;
  // Semantic % improvement vs the previous global best:
  // `(prev_best - score) / prev_best * 100`. Positive = score dropped
  // (improvement), negative = score rose (regression). Null when there
  // is no previous best.
  delta_vs_best_pct: number | null;
  // True when this iteration improved the publishing agent's own previous
  // best (not necessarily the global best).
  beats_own_best?: boolean;
  // % improvement vs the agent's own previous best. Positive = score
  // dropped (improvement). Null when the agent had no prior best.
  delta_vs_own_best_pct?: number | null;
  num_instances: number;
  is_new_best: boolean;
  hypothesis_id: string | null;
  strategy_tag?: string | null;
  title?: string | null;
  notes: string;
  // Per-track mean quality — only set on iterations published with track
  // scores. Map keys are track labels (e.g. "n_nodes=200"), values are
  // mean quality on that track.
  track_scores?: Record<string, number> | null;
  timestamp: string;
}

export interface NewGlobalBest {
  type: "new_global_best";
  // Server-side scope.
  challenge: string;
  experiment_id: string;
  agent_name: string;
  agent_id: string;
  score: number;
  improvement_pct: number;
  // % improvement over the previous global best (null if first ever)
  incremental_improvement_pct: number | null;
  num_instances: number;
  solution_data: AllRouteData | null;
  // Per-track mean quality for the new global best (see ExperimentPublished).
  track_scores?: Record<string, number> | null;
  timestamp: string;
}

export interface LeaderboardUpdate {
  type: "leaderboard_update";
  // Server-side scope. main.ts uses this to drop events whose challenge
  // doesn't match the user's viewed challenge.
  challenge: string;
  entries: LeaderboardEntry[];
  timestamp: string;
}

// Per-challenge slice carried inside StatsUpdate.per_challenge.
export interface StatsPerChallenge {
  active_agents: number;
  best_score: number | null;
  baseline_score: number | null;
  num_instances: number;
  improvement_pct: number;
  total_experiments: number;
  hypotheses_count: number;
  total_trajectories: number;
  // Distinct agents that have ever worked on this challenge.
  total_agents_in_challenge?: number;
}

export interface StatsUpdate {
  type: "stats_update";
  // Active challenge id from the swarm config; identifies which of the
  // per_challenge slices is "live".
  active_challenge: string;
  // Map: challenge_id → counters. main.ts slices this down to the user's
  // viewed challenge before populating the panels.
  per_challenge: Record<string, StatsPerChallenge>;
  // Optional flattened convenience fields — set by main.ts AFTER it
  // selects the slice for the viewed challenge. They are NOT present on
  // the wire from server.py.
  active_agents?: number;
  total_agents?: number;
  total_agents_in_challenge?: number;
  total_trajectories?: number;
  total_experiments?: number;
  hypotheses_count?: number;
  best_score?: number | null;
  baseline_score?: number | null;
  num_instances?: number;
  improvement_pct?: number;
  timestamp: string;
}

export interface TrajectoryReset {
  type: "trajectory_reset";
  challenge: string;
  agent_name: string;
  agent_id: string;
  reset_type: "fresh_start" | "adopted_inactive";
  timestamp: string;
}

export interface SwarmConfigUpdated {
  type: "swarm_config_updated";
  active_challenge: string;
  available_challenges: Record<string, unknown>;
  scoring_direction: "min" | "max";
  swarm_name: string;
  timestamp: string;
}

export interface ChatMessage {
  type: "chat_message";
  // Server-side scope.
  challenge: string;
  message_id: string;
  agent_name: string;
  agent_id: string | null;
  content: string;
  msg_type: "agent" | "milestone";
  timestamp: string;
}

export interface AdminBroadcastMsg {
  type: "admin_broadcast";
  message: string;
  priority: "normal" | "high";
  timestamp: string;
}

export interface ResetMsg {
  type: "reset";
  // Per-challenge admin reset scope. Optional because the dashboard's
  // own internal reset dispatch (e.g. on viewedChallenge change) doesn't
  // carry it.
  challenge?: string;
  timestamp: string;
}

// ── REST API response shapes (mirror of server/api_models.py) ──────────

// /api/replay row. Same shape for `compact=1` (solution_data left null).
export interface ReplayRow {
  experiment_id: string;
  agent_id: string | null;
  agent_name: string;
  score: number;
  created_at: string;
  solution_data?: unknown;
}

// /api/diversity response.
export interface DiversityTrajectory {
  trajectory_id: string;
  display_name: string;
}
export interface DiversityResponse {
  trajectories: DiversityTrajectory[];
  matrix: number[][];
}

// /api/state — partial mirror; the server still constructs this dict
// inline (server.py:612-723), so the TS type is permissive on extras.
export interface StateResponse {
  challenge: string;
  best_score?: number | null;
  num_instances: number;
  active_agents: number;
  total_agents: number;
  total_experiments: number;
  hypotheses_count: number;

  // Agent-loop view fields (?agent_id=…)
  best_algorithm_code?: string;
  best_experiment_id?: string | null;
  my_best_score?: number | null;
  my_runs?: number;
  my_improvements?: number;
  my_runs_since_improvement?: number;
  prior_hypotheses?: unknown[];
  hypothesis_recall_message?: string;
  inspiration_code?: string;
  inspiration_agent_name?: string;
  stagnation_hint?: string;
  trajectory_reset?: { type: string; prior_score?: number };

  // Dashboard view fields
  baseline_score?: number | null;
  improvement_pct?: number;
  best_solution_data?: unknown;
  best_track_scores?: Record<string, number>;
  total_trajectories?: number;
  recent_experiments?: unknown[];
  recent_hypotheses?: unknown[];
  // Recent agent registrations (global). Used to replay `agent_joined`
  // feed lines on reload / panel reset, since WS-only delivery means
  // they'd otherwise disappear permanently after any reset.
  recent_agents?: Array<{ id: string; name: string; registered_at: string }>;

  leaderboard?: LeaderboardEntry[];
}

// Panel interfaces
export interface Panel {
  init(container: HTMLElement): void;
  handleMessage(msg: WSMessage): void;
  // Optional: called when the user picks a different challenge in the
  // selector. Panels that show challenge-scoped data (leaderboard, feed,
  // chart, diversity, stats, display) should re-fetch their REST data with
  // `?challenge=…` and replay; non-challenge-scoped panels can ignore.
  setChallenge?(c: string): void;
  // Optional: called when the panel is being torn down (e.g. the display
  // panel is reconstructed because the user picked a different challenge
  // type). Panels can override to clean up timers, listeners, etc.
  dispose?(): void;
}
