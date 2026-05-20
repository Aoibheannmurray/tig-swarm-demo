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
//
// Per-challenge UI metadata (pretty name, score label, panel factory, fallback
// sub-config) lives in `challenges/registry.ts` — this file owns only the
// server-driven slice.

import {
  fallbackSubConfig,
  challengeScoreLabel,
  isKnownChallenge,
  listChallenges,
  type Challenge,
  type ChallengeSubConfig,
  type ScoringDirection,
} from "../challenges/registry";

// Re-export the challenge type aliases so callers can grab them from one
// place — but `Challenge` itself is owned by challenges/registry.ts; this
// file just consumes it.
export type { Challenge, ChallengeSubConfig, ScoringDirection };

export type SwarmType = "cpu" | "gpu";

export interface SwarmConfig {
  active_challenge: Challenge;
  available_challenges: Record<string, ChallengeSubConfig>;
  swarm_name: string;
  owner_name: string;
  swarm_type: SwarmType;
}

function buildFallback(): SwarmConfig {
  // The fallback exposes every registered challenge — when the server is
  // unreachable, the dashboard still renders something coherent. The
  // active_challenge default is the first registry entry.
  const ids = listChallenges();
  const available: Record<string, ChallengeSubConfig> = {};
  for (const id of ids) available[id] = fallbackSubConfig(id);
  return {
    active_challenge: ids[0],
    available_challenges: available,
    swarm_name: "",
    owner_name: "",
    swarm_type: "cpu",
  };
}

let current: SwarmConfig = buildFallback();
let listeners: Array<(cfg: SwarmConfig) => void> = [];

export function getSwarmConfig(): SwarmConfig {
  return current;
}

export function getActiveChallenge(): Challenge {
  return current.active_challenge;
}

export function getSwarmType(): SwarmType {
  return current.swarm_type;
}

export function getAvailableChallenges(): Challenge[] {
  // Filter to registered challenges only — if the server reports an
  // unknown challenge id (e.g. running against a dashboard build without
  // its registry entry), skip it rather than crash.
  return Object.keys(current.available_challenges).filter(isKnownChallenge);
}

export function getChallengeConfig(c: Challenge): ChallengeSubConfig {
  return current.available_challenges[c] ?? fallbackSubConfig(c);
}

// `challenge` arg is optional; defaults to the swarm's active challenge for
// callers that don't yet thread the user's viewed-challenge through. New
// dashboard code should pass the viewed challenge explicitly.
export function getDirection(challenge?: Challenge): ScoringDirection {
  return getChallengeConfig(challenge ?? current.active_challenge).scoring_direction;
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
// (or the active one when no arg is passed). Reads from the registry — the
// per-challenge label is no longer encoded as an if/else here.
export function scoreLabel(challenge?: Challenge): string {
  const c = challenge ?? current.active_challenge;
  if (!isKnownChallenge(c)) return "SCORE";
  return challengeScoreLabel(c);
}

export async function loadSwarmConfig(apiBase: string): Promise<SwarmConfig> {
  try {
    const r = await fetch(`${apiBase}/api/swarm_config`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    const reportedActive = data.active_challenge as string | undefined;
    const active = (reportedActive && isKnownChallenge(reportedActive)
      ? reportedActive
      : listChallenges()[0]) as Challenge;
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
    const swarm_type: SwarmType =
      data.swarm_type === "gpu" ? "gpu" : "cpu";
    current = {
      active_challenge: active,
      available_challenges: available,
      swarm_name: data.swarm_name ?? "",
      owner_name: data.owner_name ?? "",
      swarm_type,
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

