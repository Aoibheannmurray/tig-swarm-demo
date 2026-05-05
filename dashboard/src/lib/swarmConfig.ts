// Live swarm config: the swarm's `active_challenge` (the challenge the host
// has selected for contributors to work on) plus the per-challenge sub-configs
// (tracks, timeout, scoring_direction). Fetched once at page load and refreshed
// when the server broadcasts a `swarm_config_updated` event over the WebSocket.
//
// Every panel that renders challenge-specific content (labels, the active
// visualization, score-direction-aware deltas) should consult these helpers
// rather than hardcoding assumptions about a single challenge.
//
// `active_challenge` is server-driven (host-set). `viewedChallenge` (in
// lib/viewedChallenge.ts) is client-driven — the dashboard user picks which
// challenge's data to view via the in-page selector. Most labels and
// visualizations honour `viewedChallenge`; the active one is highlighted in
// the selector so viewers see which challenge agents are actively working on.

export type ScoringDirection = "min" | "max";

export type Challenge =
  | "satisfiability"
  | "vehicle_routing"
  | "knapsack"
  | "job_scheduling"
  | "energy_arbitrage";

export interface ChallengeSubConfig {
  tracks: Record<string, number | string>;
  timeout: number;
  scoring_direction: ScoringDirection;
  has_initial_algorithm: boolean;
}

export interface SwarmConfig {
  active_challenge: Challenge;
  available_challenges: Record<string, ChallengeSubConfig>;
  // Back-compat flat fields — populated from the active challenge's sub-config.
  challenge: Challenge;
  scoring_direction: ScoringDirection;
  tracks: Record<string, number | string>;
  timeout: number;
  swarm_name: string;
  owner_name: string;
}

const FALLBACK_CH: ChallengeSubConfig = {
  tracks: {},
  timeout: 5,
  scoring_direction: "max",
  has_initial_algorithm: false,
};

const FALLBACK: SwarmConfig = {
  active_challenge: "vehicle_routing",
  available_challenges: {
    satisfiability: { ...FALLBACK_CH },
    vehicle_routing: { ...FALLBACK_CH },
    knapsack: { ...FALLBACK_CH },
    job_scheduling: { ...FALLBACK_CH },
    energy_arbitrage: { ...FALLBACK_CH },
  },
  challenge: "vehicle_routing",
  scoring_direction: "max",
  tracks: {},
  timeout: 5,
  swarm_name: "",
  owner_name: "",
};

let current: SwarmConfig = FALLBACK;
let listeners: Array<(cfg: SwarmConfig) => void> = [];

export function getSwarmConfig(): SwarmConfig {
  return current;
}

export function getActiveChallenge(): Challenge {
  return current.active_challenge;
}

export function getAvailableChallenges(): Challenge[] {
  return Object.keys(current.available_challenges) as Challenge[];
}

export function getChallengeConfig(c: Challenge): ChallengeSubConfig {
  return current.available_challenges[c] ?? FALLBACK_CH;
}

// `challenge` arg is optional; defaults to the swarm's active challenge for
// callers that don't yet thread the user's viewed-challenge through. New
// dashboard code should pass the viewed challenge explicitly.
export function getDirection(challenge?: Challenge): ScoringDirection {
  if (challenge) return getChallengeConfig(challenge).scoring_direction;
  return current.scoring_direction;
}

export function isMin(challenge?: Challenge): boolean {
  return getDirection(challenge) === "min";
}

export function isMax(challenge?: Challenge): boolean {
  return getDirection(challenge) === "max";
}

// Returns true if `candidate` beats `prior` in the given challenge's direction.
export function isBetter(candidate: number, prior: number, challenge?: Challenge): boolean {
  return getDirection(challenge) === "max" ? candidate > prior : candidate < prior;
}

// Score label for stats / leaderboard headers, scoped to the given challenge
// (or the active one when no arg is passed).
export function scoreLabel(challenge?: Challenge): string {
  const c = challenge ?? current.active_challenge;
  if (c === "vehicle_routing") return "DISTANCE";
  if (c === "satisfiability") return "QUALITY";
  if (c === "knapsack") return "VALUE";
  if (c === "job_scheduling") return "MAKESPAN";
  if (c === "energy_arbitrage") return "PROFIT";
  return "SCORE";
}

export async function loadSwarmConfig(apiBase: string): Promise<SwarmConfig> {
  try {
    const r = await fetch(`${apiBase}/api/swarm_config`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    const active = (data.active_challenge ?? data.challenge ?? FALLBACK.active_challenge) as Challenge;
    const available: Record<string, ChallengeSubConfig> = {};
    const raw = (data.available_challenges ?? {}) as Record<string, any>;
    for (const [name, sub] of Object.entries(raw)) {
      available[name] = {
        tracks: sub?.tracks ?? {},
        timeout: typeof sub?.timeout === "number" ? sub.timeout : 5,
        scoring_direction: sub?.scoring_direction === "min" ? "min" : "max",
        has_initial_algorithm: !!sub?.has_initial_algorithm,
      };
    }
    // If the server returned the legacy flat shape only, synthesise an
    // available_challenges entry for the active challenge so dashboard
    // helpers don't NPE.
    if (Object.keys(available).length === 0) {
      available[active] = {
        tracks: data.tracks ?? {},
        timeout: typeof data.timeout === "number" ? data.timeout : 5,
        scoring_direction: data.scoring_direction === "min" ? "min" : "max",
        has_initial_algorithm: false,
      };
    }
    const activeSub = available[active] ?? FALLBACK_CH;
    current = {
      active_challenge: active,
      available_challenges: available,
      challenge: active,
      scoring_direction: activeSub.scoring_direction,
      tracks: activeSub.tracks,
      timeout: activeSub.timeout,
      swarm_name: data.swarm_name ?? "",
      owner_name: data.owner_name ?? "",
    };
    notify();
  } catch (e) {
    console.warn("loadSwarmConfig: falling back to defaults", e);
  }
  return current;
}

export function onSwarmConfigChange(fn: (cfg: SwarmConfig) => void): () => void {
  listeners.push(fn);
  return () => {
    listeners = listeners.filter((l) => l !== fn);
  };
}

function notify(): void {
  for (const l of listeners) {
    try {
      l(current);
    } catch (e) {
      console.error("swarm config listener error", e);
    }
  }
}

// Wire up to the WebSocket so dashboards see the host switching the
// active challenge mid-experiment without a manual refresh. Re-fetches
// the full config (the WS event carries only a subset).
export function handleWsEvent(apiBase: string, msg: any): void {
  if (msg && msg.type === "swarm_config_updated") {
    void loadSwarmConfig(apiBase);
  }
}
