// Per-challenge "best known" reference for the dashboard's gap-vs-baseline
// readout. For VRP we use the average of the literature BKS upper bounds
// across the 24 Homberger-Gehring 400-customer instances. For the other
// challenges the upstream evaluator already returns a quality score
// normalised against an internal greedy/baseline (so the gap-vs-BKS
// concept is less meaningful), and we simply report null as the reference.

import { getSwarmConfig } from "./swarmConfig";

export const VRP_BKS_AVERAGE = 6679.775;
export const VRP_BKS_INSTANCE_COUNT = 24;

export function bksReference(): number | null {
  return getSwarmConfig().active_challenge === "vehicle_routing" ? VRP_BKS_AVERAGE : null;
}

export function bksGapPct(score: number): number {
  const ref = bksReference();
  if (ref === null || ref === 0) return 0;
  return ((score - ref) / ref) * 100;
}
