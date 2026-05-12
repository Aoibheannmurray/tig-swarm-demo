// Single source of truth for per-challenge dashboard wiring.
//
// Adding a new challenge to the dashboard:
//   1. Drop a `challenges/<challenge_id>.ts` file with a panel class that
//      extends `DisplayPanelBase` (see ./base.ts).
//   2. Import it and add ONE entry to the CHALLENGES table below — the
//      union type, the score label, the panel factory, the fallback
//      sub-config, and the pretty name are all read off the entry.
//
// Server-driven config (active_challenge, available_challenges, tracks,
// timeout, scoring_direction) still flows through swarmConfig.ts; this
// file only owns the *client-side* metadata that doesn't come from the
// wire.

import type { Panel } from "../types";
import { SolutionPanel } from "./vehicle_routing";
import { GanttPanel } from "./job_scheduling";
import { KnapsackPanel } from "./knapsack";
import { EnergyPanel } from "./energy_arbitrage";
import { SatPanel } from "./satisfiability";
import { HypergraphPanel } from "./hypergraph";
import { NeuralnetPanel } from "./neuralnet_optimizer";

export type ScoringDirection = "min" | "max";

export interface ChallengeSubConfig {
  tracks: Record<string, number | string>;
  timeout: number;
  scoring_direction: ScoringDirection;
  has_initial_algorithm: boolean;
}

interface ChallengeEntry {
  pretty: string;
  scoreLabel: string;
  // Builds the per-challenge visualization panel. The id is passed in so
  // the panel can scope its REST fetches with `?challenge=…` and decide
  // when the LIVE button should switch challenges — without hardcoding
  // the literal in panel code. Typed as `string` here (not Challenge) to
  // avoid the self-referential const cycle: Challenge = keyof typeof
  // CHALLENGES, so an entry that references Challenge in its value type
  // would close the loop. Callers in this module narrow back to
  // Challenge before invoking.
  panelFactory: (id: string) => Panel;
  // Used by swarmConfig.ts FALLBACK and as the per-challenge defaults
  // when the server hasn't reported a sub-config for this challenge.
  fallback: ChallengeSubConfig;
}

const FALLBACK_SUB: ChallengeSubConfig = {
  tracks: {},
  timeout: 5,
  scoring_direction: "max",
  has_initial_algorithm: false,
};

// CHALLENGES: keys are the challenge IDs used on the wire and in storage.
// `as const` narrows the keys to literals, so `keyof typeof CHALLENGES`
// becomes the TypeScript Challenge union — no separate enum required.
export const CHALLENGES = {
  satisfiability: {
    pretty: "Satisfiability",
    scoreLabel: "QUALITY",
    panelFactory: (id: string) => new SatPanel(id),
    fallback: { ...FALLBACK_SUB },
  },
  vehicle_routing: {
    pretty: "Vehicle Routing",
    scoreLabel: "DISTANCE",
    panelFactory: (id: string) => new SolutionPanel(id),
    fallback: { ...FALLBACK_SUB },
  },
  knapsack: {
    pretty: "Knapsack",
    scoreLabel: "VALUE",
    panelFactory: (id: string) => new KnapsackPanel(id),
    fallback: { ...FALLBACK_SUB },
  },
  job_scheduling: {
    pretty: "Job Scheduling",
    scoreLabel: "MAKESPAN",
    panelFactory: (id: string) => new GanttPanel(id),
    fallback: { ...FALLBACK_SUB },
  },
  energy_arbitrage: {
    pretty: "Energy Arbitrage",
    scoreLabel: "PROFIT",
    panelFactory: (id: string) => new EnergyPanel(id),
    fallback: { ...FALLBACK_SUB },
  },
  hypergraph: {
    pretty: "Hypergraph",
    scoreLabel: "QUALITY",
    panelFactory: (id: string) => new HypergraphPanel(id),
    fallback: { ...FALLBACK_SUB },
  },
  neuralnet_optimizer: {
    pretty: "Neural Net Optimizer",
    scoreLabel: "QUALITY",
    panelFactory: (id: string) => new NeuralnetPanel(id),
    fallback: { ...FALLBACK_SUB },
  },
} as const satisfies Record<string, ChallengeEntry>;

export type Challenge = keyof typeof CHALLENGES;

export function listChallenges(): Challenge[] {
  return Object.keys(CHALLENGES) as Challenge[];
}

export function getChallengeEntry(c: Challenge): ChallengeEntry {
  return CHALLENGES[c];
}

export function isKnownChallenge(c: string): c is Challenge {
  return c in CHALLENGES;
}

export function buildPanelFor(c: Challenge): Panel {
  return CHALLENGES[c].panelFactory(c);
}

export function prettyName(c: Challenge): string {
  return CHALLENGES[c].pretty;
}

export function challengeScoreLabel(c: Challenge): string {
  return CHALLENGES[c].scoreLabel;
}

export function fallbackSubConfig(c: Challenge): ChallengeSubConfig {
  return { ...CHALLENGES[c].fallback };
}
