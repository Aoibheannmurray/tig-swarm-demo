import { describe, it, expect } from "vitest";
import { isMessageForChallenge } from "./messageScope";
import type { WSMessage } from "../types";

const chat = (challenge: string): WSMessage => ({
  type: "chat_message",
  challenge,
  message_id: "m1",
  agent_name: "agent",
  agent_id: "a1",
  content: "hi",
  msg_type: "agent",
  timestamp: "2026-01-01T00:00:00Z",
});

describe("isMessageForChallenge", () => {
  it("passes a challenge-scoped message whose challenge matches", () => {
    expect(isMessageForChallenge(chat("knapsack"), "knapsack")).toBe(true);
  });

  it("drops a challenge-scoped message for a different challenge", () => {
    expect(isMessageForChallenge(chat("knapsack"), "job_scheduling")).toBe(false);
  });

  it("passes a scoped type with no challenge field (falls through)", () => {
    const msg: WSMessage = {
      type: "hypothesis_proposed",
      hypothesis_id: "h1",
      agent_name: "agent",
      agent_id: "a1",
      title: "t",
      description: "d",
      strategy_tag: "other",
      parent_hypothesis_id: null,
      timestamp: "2026-01-01T00:00:00Z",
    };
    expect(isMessageForChallenge(msg, "knapsack")).toBe(true);
  });

  it("passes unscoped types regardless of the viewed challenge", () => {
    const msg: WSMessage = {
      type: "agent_joined",
      agent_id: "a1",
      agent_name: "agent",
      timestamp: "2026-01-01T00:00:00Z",
    };
    expect(isMessageForChallenge(msg, "knapsack")).toBe(true);
  });

  // The dashboard's own internal reset (dispatched on viewedChallenge change)
  // carries no `challenge` field and must always pass so panels clear; a
  // server-side per-challenge reset carries one and is filtered like any
  // other scoped event.
  it("always passes a reset without a challenge; filters one with it", () => {
    const internalReset: WSMessage = { type: "reset", timestamp: "2026-01-01T00:00:00Z" };
    expect(isMessageForChallenge(internalReset, "knapsack")).toBe(true);

    const serverReset: WSMessage = {
      type: "reset",
      challenge: "knapsack",
      timestamp: "2026-01-01T00:00:00Z",
    };
    expect(isMessageForChallenge(serverReset, "knapsack")).toBe(true);
    expect(isMessageForChallenge(serverReset, "job_scheduling")).toBe(false);
  });
});
