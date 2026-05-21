// User-driven currently-viewed challenge.
//
// `swarmConfig.active_challenge` is the swarm-wide challenge agents are
// actively working on (server-controlled, host-set). `viewedChallenge` is
// the user's view selector — the dashboard renders that challenge's
// leaderboard, feed, ideas, and visualization, regardless of which one
// agents are currently working on. So historical leaderboards for inactive
// challenges remain browsable.
//
// Default for first-time visitors: the swarm's active challenge. Persisted
// to localStorage so a user's choice sticks across page loads (and across
// pages, since each entry-point HTML loads this same module).

import {
  getActiveChallenge,
  getAvailableChallenges,
  onSwarmConfigChange,
} from "./swarmConfig";
import type { Challenge } from "../challenges/registry";

const STORAGE_KEY = "viewedChallenge";

let current: Challenge | null = null;
// Tracks where `current` came from so we can decide whether a later
// swarm-config update should override it:
//   "user"   — read from localStorage (explicit prior choice; do NOT override)
//   "active" — derived from the swarm's active_challenge fallback (override
//              when the real active_challenge lands)
//   null     — not resolved yet
let currentSource: "user" | "active" | null = null;
let listeners: Array<(c: Challenge) => void> = [];

function readStored(): Challenge | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    return raw as Challenge;
  } catch {
    return null;
  }
}

function notify(c: Challenge): void {
  for (const l of listeners) {
    try {
      l(c);
    } catch (e) {
      console.error("viewedChallenge listener error", e);
    }
  }
}

export function getViewedChallenge(): Challenge {
  if (current !== null) return current;
  const stored = readStored();
  // Validate against the swarm's available list (in case the host removed a
  // challenge or this is a stale localStorage value).
  const available = getAvailableChallenges();
  if (stored && available.includes(stored)) {
    current = stored;
    currentSource = "user";
    return current;
  }
  current = getActiveChallenge();
  currentSource = "active";
  return current;
}

export function setViewedChallenge(c: Challenge): void {
  if (current === c && currentSource === "user") return;
  current = c;
  currentSource = "user";
  try {
    localStorage.setItem(STORAGE_KEY, c);
  } catch {
    // localStorage may be disabled in some embedded contexts; non-fatal.
  }
  notify(c);
}

export function onViewedChallengeChange(fn: (c: Challenge) => void): () => void {
  listeners.push(fn);
  return () => {
    listeners = listeners.filter((l) => l !== fn);
  };
}

// When the real swarm config lands (or the host switches active_challenge
// at runtime), update `current` if and only if the cached value came from
// the fallback — i.e. the user has no explicit localStorage preference.
// This fixes pages that bootstrap panels synchronously and so resolve
// `getViewedChallenge()` before the network call to /api/swarm_config
// finishes — without this, those pages cache the registry-order fallback
// (`satisfiability`) and never recover.
onSwarmConfigChange(() => {
  if (currentSource !== "active") return;
  const newActive = getActiveChallenge();
  if (newActive === current) return;
  current = newActive;
  notify(newActive);
});
