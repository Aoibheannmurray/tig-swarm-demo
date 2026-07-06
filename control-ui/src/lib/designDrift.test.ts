// Drift checks for the two DELIBERATE cross-package copies of dashboard
// design values. These are copies on purpose (the packages build
// independently), so instead of codegen we assert at test time that the
// copies still agree. If one of these tests fails, update BOTH copies.

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const controlUiRoot = path.resolve(here, "../..");
const dashboardRoot = path.resolve(controlUiRoot, "../dashboard");

const read = (p: string) => readFileSync(p, "utf8");

// Pull the custom properties out of the first `:root { ... }` block.
function rootTokens(css: string): Map<string, string> {
  const root = css.match(/:root\s*\{([\s\S]*?)\n\}/);
  if (!root) throw new Error("no :root block found");
  const tokens = new Map<string, string>();
  for (const m of root[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    tokens.set(m[1], m[2].trim());
  }
  return tokens;
}

describe("design tokens stay in sync with the dashboard", () => {
  it("every token shared by tokens.css and style.css has the same value", () => {
    const ours = rootTokens(read(path.join(controlUiRoot, "src/tokens.css")));
    const theirs = rootTokens(read(path.join(dashboardRoot, "src/style.css")));

    const shared = [...ours.keys()].filter((k) => theirs.has(k));
    // Guard against the parser silently matching nothing (which would make
    // the value comparison below vacuous). The shared block is ~36 tokens.
    expect(shared.length).toBeGreaterThanOrEqual(30);

    for (const key of shared) {
      expect
        .soft(ours.get(key), `${key} drifted between control-ui/src/tokens.css and dashboard/src/style.css — update both copies`)
        .toBe(theirs.get(key));
    }
  });
});

describe("LogStream palette stays in sync with the dashboard", () => {
  it("LogStream.svelte's 8 colors equal the first 8 of dashboard colors.ts", () => {
    const extractHexes = (source: string): string[] => {
      const decl = source.match(/const PALETTE = \[([\s\S]*?)\];/);
      if (!decl) throw new Error("no PALETTE declaration found");
      return decl[1].match(/#[0-9A-Fa-f]{6}/g) ?? [];
    };

    const logStream = extractHexes(
      read(path.join(controlUiRoot, "src/components/LogStream.svelte")),
    );
    const dashboard = extractHexes(
      read(path.join(dashboardRoot, "src/lib/colors.ts")),
    );

    expect(logStream).toHaveLength(8);
    expect(dashboard.length).toBeGreaterThanOrEqual(8);
    expect(
      logStream,
      "LogStream.svelte palette drifted from dashboard/src/lib/colors.ts PALETTE[0..8] — update both copies",
    ).toEqual(dashboard.slice(0, 8));
  });
});
