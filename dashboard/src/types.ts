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
  runs: number;
  improvements: number;
  runs_since_improvement: number;
  current_score: number | null;
  best_ever_score: number | null;
  num_trajectories: number;
  tacit_knowledge_count: number;
  inspiration_count: number;
  active: boolean;
}

// WebSocket message types
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
  | ResetMsg;

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
  entries: LeaderboardEntry[];
  timestamp: string;
}

export interface StatsUpdate {
  type: "stats_update";
  active_agents: number;
  // Total number of agents that have ever registered.
  total_agents?: number;
  total_experiments: number;
  hypotheses_count: number;
  // Both per-instance averages. null until the first feasible experiment
  // lands — there is no reference point before then.
  best_score: number | null;
  baseline_score: number | null;
  num_instances: number;
  improvement_pct: number;
  timestamp: string;
}

export interface ChatMessage {
  type: "chat_message";
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
  timestamp: string;
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
