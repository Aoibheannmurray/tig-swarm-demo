import { describe, it, expect } from "vitest";
// `?raw` rather than node:fs — @types/node isn't a dependency here, and the
// build's tsc pass covers test files too.
import css from "../style.css?inline";
import leaderboardSrc from "../panels/leaderboard.ts?raw";
import chartSrc from "../panels/chart.ts?raw";

// A class name typo'd between the panel and the stylesheet type-checks fine,
// builds fine, and ships an unstyled row — the mainnet entry would render as a
// plain agent row with no accent and no rules around it, quietly undoing the
// one thing that marks it as a different kind of entry. There's no DOM test
// environment here, so assert the contract statically.

describe("mainnet baseline styling", () => {
  it("styles every class the leaderboard ghost row emits", () => {
    const panel = leaderboardSrc;
    const used = [...panel.matchAll(/\b(lb-mainnet[\w-]*)\b/g)].map((m) => m[1]);
    expect(used.length).toBeGreaterThan(0);
    for (const cls of new Set(used)) {
      expect(css, `.${cls} is emitted but never styled`).toContain(`.${cls}`);
    }
  });

  it("keeps the row visually distinct from an agent row", () => {
    // The accent and the bounding rules are what say "this is the bar, not a
    // participant". Losing them is a silent regression of the whole point.
    const block = css.slice(css.indexOf(".lb-mainnet {"));
    expect(block).toContain("--color-accent");
    expect(block).toContain("border-top");
    expect(block).toContain("border-bottom");
  });

  it("draws the chart threshold in the same accent as the row", () => {
    // Both displays refer to one thing; different colours would read as two.
    const chart = chartSrc;
    expect(chart).toContain('token("--color-accent"');
    expect(chart).toContain("stroke-dasharray");
  });
});
