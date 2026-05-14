// Shared helpers for the per-challenge solution panels' "LIVE →" button.
//
// Each panel has its own history navigation (prev / next through global
// bests) and a "LIVE →" affordance to jump to the latest entry. The
// button has dual semantics depending on whether the user is viewing
// the swarm's active challenge:
//
//   - Non-active challenge: clicking LIVE switches the viewed challenge
//     to the active one. This is the user's "take me to whatever the
//     swarm is currently working on" path. The button is always shown
//     here so the affordance is discoverable.
//
//   - Active challenge: clicking LIVE jumps to the latest historical
//     entry — its original purpose, used after the user steps back
//     through breakthroughs with the < / > arrows. Hidden when already
//     at the latest.

import { getActiveChallenge } from "./swarmConfig";
import type { Challenge } from "../challenges/registry";
import { setViewedChallenge } from "./viewedChallenge";

/**
 * Returns true if the click was handled by switching to the active
 * challenge. The caller should bail in that case; otherwise fall
 * through to its own "jump to latest history" behaviour.
 */
export function liveSwitchToActive(thisChallenge: Challenge): boolean {
  const active = getActiveChallenge();
  if (active && active !== thisChallenge) {
    setViewedChallenge(active);
    return true;
  }
  return false;
}

/**
 * Visibility rule for the panel's "LIVE →" button.
 */
export function shouldShowLiveButton(
  thisChallenge: Challenge,
  atLatestHistoryEntry: boolean,
): boolean {
  const active = getActiveChallenge();
  if (active && active !== thisChallenge) return true;
  return !atLatestHistoryEntry;
}
