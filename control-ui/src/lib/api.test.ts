import { describe, it, expect } from "vitest";
import { createHash } from "node:crypto";
import { deriveInvitePassword } from "./api";

// Node 18+ exposes globalThis.crypto.subtle, which deriveInvitePassword uses.

describe("deriveInvitePassword", () => {
  it("matches setup.py's sha256(username:base)", async () => {
    const got = await deriveInvitePassword("alice", "base123");
    const expected = createHash("sha256").update("alice:base123").digest("hex");
    expect(got).toBe(expected);
  });

  it("is deterministic and 64 hex chars", async () => {
    const a = await deriveInvitePassword("bob", "s3cret");
    const b = await deriveInvitePassword("bob", "s3cret");
    expect(a).toBe(b);
    expect(a).toMatch(/^[0-9a-f]{64}$/);
  });
});
