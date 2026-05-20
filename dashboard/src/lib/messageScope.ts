import type { WSMessage } from "../types";

// Event types that carry per-challenge data. When `msg.challenge` is set and
// doesn't match the user's viewed challenge, the dispatcher drops the event
// so panels render consistent state. Other events (agent_joined,
// swarm_config_updated, admin_broadcast, the global slice of stats_update)
// pass through unfiltered.
//
// `reset` is included because /api/admin/reset_challenge broadcasts
// {type:"reset", challenge:"…"} — without filtering, a reset on knapsack
// would clear panels viewing job_scheduling. The dashboard's own internal
// {type:"reset"} (dispatched on viewedChallenge change) has no `challenge`
// field and so passes the filter — which is what we want, the panels need
// to clear.
const CHALLENGE_SCOPED_TYPES = new Set<WSMessage["type"]>([
  "experiment_published",
  "hypothesis_proposed",
  "hypothesis_status_changed",
  "new_global_best",
  "leaderboard_update",
  "chat_message",
  "trajectory_reset",
  "reset",
]);

// Returns true if the message should be dispatched to panels viewing the
// given challenge. Falls through (returns true) when the message either
// isn't challenge-scoped or has no `challenge` field set.
export function isMessageForChallenge(msg: WSMessage, viewedChallenge: string): boolean {
  if (!CHALLENGE_SCOPED_TYPES.has(msg.type)) return true;
  const msgChallenge = (msg as { challenge?: string }).challenge;
  if (!msgChallenge) return true;
  return msgChallenge === viewedChallenge;
}
