import { describe, it, expect } from "vitest";
import {
  isComparable, statusLabel, beatsBaseline, baselineRank, countBeating, deltaPct,
} from "./mainnetBaseline";
import type { LeaderboardEntry, MainnetBaseline } from "../types";

const base: MainnetBaseline = {
  algorithm: "hgs_advance",
  adoption_pct: 41.2,
  score: 177004,
  feasible: true,
  status: "ready",
  measured_by: "agent:a1",
  benchmarked_at: "2026-07-27T00:00:00Z",
  stale: false,
  direction: "max",
};

const entry = (name: string, score: number | null): LeaderboardEntry =>
  ({ agent_id: name, agent_name: name, best_ever_score: score } as any);
const bestOf = (e: LeaderboardEntry) => e.best_ever_score;

describe("isComparable", () => {
  it("accepts a measured, feasible, current score", () => {
    expect(isComparable(base)).toBe(true);
  });

  it("rejects anything without an honest number behind it", () => {
    // Each of these would otherwise put a misleading line on the chart.
    expect(isComparable(null)).toBe(false);
    expect(isComparable({ ...base, status: "pending", score: null })).toBe(false);
    expect(isComparable({ ...base, status: "requested", score: null })).toBe(false);
    expect(isComparable({ ...base, status: "unavailable" })).toBe(false);
    expect(isComparable({ ...base, feasible: false })).toBe(false);
    // Measured, but the host has since changed the instance set.
    expect(isComparable({ ...base, stale: true })).toBe(false);
  });
});

describe("statusLabel", () => {
  it("explains every non-comparable state, and stays quiet otherwise", () => {
    expect(statusLabel(base)).toBeNull();
    expect(statusLabel({ ...base, status: "pending", score: null }))
      .toBe("not measured yet");
    expect(statusLabel({ ...base, status: "requested", score: null }))
      .toBe("measuring…");
    expect(statusLabel({ ...base, status: "unavailable" }))
      .toBe("no mainnet algorithm");
    expect(statusLabel({ ...base, stale: true }))
      .toBe("instances changed since measured");
  });
});

describe("beatsBaseline", () => {
  it("respects scoring direction", () => {
    expect(beatsBaseline(180000, 177004, "max")).toBe(true);
    expect(beatsBaseline(170000, 177004, "max")).toBe(false);
    // min-direction challenges: lower is better.
    expect(beatsBaseline(170000, 177004, "min")).toBe(true);
    expect(beatsBaseline(180000, 177004, "min")).toBe(false);
  });

  it("does not count a tie as beaten", () => {
    // Matching mainnet is not passing it.
    expect(beatsBaseline(177004, 177004, "max")).toBe(false);
    expect(beatsBaseline(177004, 177004, "min")).toBe(false);
  });

  it("treats an agent with no score as not beating it", () => {
    expect(beatsBaseline(null, 177004, "max")).toBe(false);
  });
});

describe("baselineRank", () => {
  const entries = [
    entry("misty-buffalo", 179141),
    entry("opus-007", 178502),
    entry("fable-explorer", 176880),
    entry("deepseek002", 174201),
    entry("newcomer", null),
  ];

  it("slots in below everyone who has beaten it", () => {
    // Two agents ahead → mainnet is 3rd.
    expect(baselineRank(entries, 177004, "max", bestOf)).toBe(3);
  });

  it("ranks first while nothing has beaten it", () => {
    expect(baselineRank(entries, 200000, "max", bestOf)).toBe(1);
  });

  it("ranks last once everyone is past it", () => {
    expect(baselineRank(entries, 1, "max", bestOf)).toBe(5);
  });

  it("inverts for min-direction challenges", () => {
    // Lower is better, so 174201 and 176880 are the ones ahead.
    expect(baselineRank(entries, 177004, "min", bestOf)).toBe(3);
  });
});

describe("countBeating", () => {
  it("counts only agents strictly past the bar", () => {
    const entries = [entry("a", 180000), entry("b", 177004), entry("c", 170000)];
    expect(countBeating(entries, 177004, "max", bestOf)).toBe(1);
  });
});

describe("deltaPct", () => {
  it("is positive when better, in both directions", () => {
    expect(deltaPct(179141, 177004, "max")).toBeCloseTo(1.207, 2);
    expect(deltaPct(174867, 177004, "min")).toBeCloseTo(1.207, 2);
  });

  it("is negative when worse", () => {
    expect(deltaPct(176880, 177004, "max")).toBeCloseTo(-0.07, 2);
  });

  it("returns null rather than dividing by zero", () => {
    expect(deltaPct(100, 0, "max")).toBeNull();
    expect(deltaPct(null, 177004, "max")).toBeNull();
  });
});
