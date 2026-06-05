import { describe, it, expect } from "vitest";
import { formatScore } from "./format";

describe("formatScore", () => {
  it("renders null / NaN as an em dash", () => {
    expect(formatScore(null)).toBe("—");
    expect(formatScore(undefined)).toBe("—");
    expect(formatScore(NaN)).toBe("—");
  });

  it("formats documented happy-path values", () => {
    expect(formatScore(4_879_698.2)).toBe("4.88M");
    expect(formatScore(157_094.9)).toBe("157k");
  });

  it("keeps small magnitudes readable", () => {
    expect(formatScore(0)).toBe("0.0");
    expect(formatScore(12.34)).toBe("12.3");
    expect(formatScore(999)).toBe("999.0");
  });

  // The regression: rounding must not strand a value in a tier whose suffix is
  // wrong. 999_600 used to render "1000k" instead of rolling over to "1.0M".
  it("carries across the k→M boundary instead of showing 1000k", () => {
    expect(formatScore(999_600)).toBe("1.00M");
    expect(formatScore(999_999)).toBe("1.00M");
    expect(formatScore(999_499)).toBe("999k"); // stays in k — rounds down
    expect(formatScore(1_000_000)).toBe("1.00M");
  });

  it("carries across the M→B boundary", () => {
    expect(formatScore(999_996_000)).toBe("1.00B");
    expect(formatScore(1_000_000_000)).toBe("1.00B");
    expect(formatScore(999_000_000)).toBe("999.00M");
  });

  it("carries across the k sub-tier boundaries", () => {
    // 9_999 → 9.999 rounds to 10.00 in the 2-decimal tier; must render in the
    // 1-decimal tier as "10.0k", not "10.00k".
    expect(formatScore(9_999)).toBe("10.0k");
    // 9_995 stays put — 9.995 rounds down to 9.99 (no overflow).
    expect(formatScore(9_995)).toBe("9.99k");
    // 99_950 → 99.95 rounds to 100.0; must render as integer-k "100k".
    expect(formatScore(99_950)).toBe("100k");
  });

  it("normalises near-zero negatives so no phantom minus appears", () => {
    expect(formatScore(-0.04)).toBe("0.0");
    expect(formatScore(-0.001)).toBe("0.0");
  });

  it("formats genuine negative scores with a sign", () => {
    expect(formatScore(-1_500_000)).toBe("-1.50M");
    expect(formatScore(-2_500)).toBe("-2.50k");
  });
});
