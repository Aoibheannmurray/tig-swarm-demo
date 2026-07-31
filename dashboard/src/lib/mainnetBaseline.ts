// Where the TIG mainnet algorithm sits among the swarm's own agents.
//
// The dashboard shows the mainnet score two ways — a threshold line on the
// benchmark chart and a ranked row in the leaderboard — and both need the same
// question answered: given these agents' scores, is mainnet beaten, and by
// whom? Kept as pure functions so that logic is testable without a DOM.

import type { LeaderboardEntry, MainnetBaseline } from "../types";

// A baseline worth drawing: measured, feasible, and earned on the instance set
// the swarm is still running. Anything else (pending, queued, unavailable, or
// stale after a host edited tracks) has no number that can honestly be
// compared against an agent's score.
export function isComparable(b: MainnetBaseline | null | undefined): boolean {
  return !!b && b.status === "ready" && b.score !== null && b.feasible && !b.stale;
}

// Short label for the status when there's no number to show. Returns null when
// the baseline IS comparable — callers render the score instead.
export function statusLabel(b: MainnetBaseline | null | undefined): string | null {
  if (!b || isComparable(b)) return null;
  if (b.stale && b.status === "ready") return "instances changed since measured";
  switch (b.status) {
    case "pending": return "not measured yet";
    case "requested": return "measuring…";
    case "unavailable": return "no mainnet algorithm";
    default: return null;
  }
}

export function beatsBaseline(
  score: number | null,
  baselineScore: number,
  direction: "min" | "max",
): boolean {
  if (score === null) return false;
  return direction === "max" ? score > baselineScore : score < baselineScore;
}

// The 1-based rank the mainnet row occupies once inserted among `entries`,
// which must already be sorted best-first. Rank 1 means nothing has beaten it.
//
// Ties count as NOT beaten: matching mainnet is not passing it, and calling it
// beaten on an equal score would be the kind of small lie that makes people
// stop trusting the number.
export function baselineRank(
  entries: LeaderboardEntry[],
  baselineScore: number,
  direction: "min" | "max",
  scoreOf: (e: LeaderboardEntry) => number | null,
): number {
  let ahead = 0;
  for (const e of entries) {
    if (beatsBaseline(scoreOf(e), baselineScore, direction)) ahead++;
  }
  return ahead + 1;
}

// How many agents have cleared the bar — the ghost row's hover tooltip
// ("2 of 9 agents have beaten mainnet").
export function countBeating(
  entries: LeaderboardEntry[],
  baselineScore: number,
  direction: "min" | "max",
  scoreOf: (e: LeaderboardEntry) => number | null,
): number {
  return entries.filter((e) => beatsBaseline(scoreOf(e), baselineScore, direction)).length;
}

// Signed percentage difference of `score` against the baseline, expressed so
// that positive always means better regardless of scoring direction. Null when
// there's nothing to compare or the baseline is zero (degenerate).
export function deltaPct(
  score: number | null,
  baselineScore: number,
  direction: "min" | "max",
): number | null {
  if (score === null || baselineScore === 0) return null;
  const raw = direction === "max"
    ? (score - baselineScore) / Math.abs(baselineScore)
    : (baselineScore - score) / Math.abs(baselineScore);
  return raw * 100;
}
