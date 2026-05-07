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

import { getActiveChallenge, getAvailableChallenges } from "./swarmConfig";
import type { Challenge } from "./challengeRegistry";

const STORAGE_KEY = "viewedChallenge";

let current: Challenge | null = null;
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

export function getViewedChallenge(): Challenge {
  if (current !== null) return current;
  const stored = readStored();
  // Validate against the swarm's available list (in case the host removed a
  // challenge or this is a stale localStorage value).
  const available = getAvailableChallenges();
  if (stored && available.includes(stored)) {
    current = stored;
    return current;
  }
  current = getActiveChallenge();
  return current;
}

export function setViewedChallenge(c: Challenge): void {
  if (current === c) return;
  current = c;
  try {
    localStorage.setItem(STORAGE_KEY, c);
  } catch {
    // localStorage may be disabled in some embedded contexts; non-fatal.
  }
  for (const l of listeners) {
    try {
      l(c);
    } catch (e) {
      console.error("viewedChallenge listener error", e);
    }
  }
}

export function onViewedChallengeChange(fn: (c: Challenge) => void): () => void {
  listeners.push(fn);
  return () => {
    listeners = listeners.filter((l) => l !== fn);
  };
}
