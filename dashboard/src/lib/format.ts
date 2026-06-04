// Compact score formatting: 4879698.2 → "4.88M", 157094.9 → "157k".
// Used in the leaderboard, chart axes, feed and strategy leaderboard so
// large baseline-relative quality scores stay readable at a glance.

// Magnitude tiers ordered smallest → largest. `max` is the exclusive upper
// bound (in raw score units) a tier covers; `divisor`/`decimals`/`suffix`
// control how it renders. The final tier's `max` is Infinity so every value
// lands somewhere.
//
//   <1k          one decimal, no suffix (don't round small values to 0)
//   1k–9.99k     two decimals
//   10k–99.9k    one decimal
//   100k–999k    integer k (keeps narrow leaderboard cells uncrowded)
//   1M–999M      two decimals
//   ≥1B          two decimals
const SCORE_TIERS: { max: number; divisor: number; decimals: number; suffix: string }[] = [
  { max: 1e3, divisor: 1, decimals: 1, suffix: "" },
  { max: 1e4, divisor: 1e3, decimals: 2, suffix: "k" },
  { max: 1e5, divisor: 1e3, decimals: 1, suffix: "k" },
  { max: 1e6, divisor: 1e3, decimals: 0, suffix: "k" },
  { max: 1e9, divisor: 1e6, decimals: 2, suffix: "M" },
  { max: Infinity, divisor: 1e9, decimals: 2, suffix: "B" },
];

export function formatScore(score: number | null | undefined): string {
  if (score == null || Number.isNaN(score)) return "—";
  const abs = Math.abs(score);

  // Pick the tier by magnitude, then RE-CHECK after rounding: if toFixed has
  // pushed the displayed value up to the tier's own ceiling, carry into the
  // next tier. Without this, a score like 999_600 is selected into the "k"
  // tier (it's < 1e6), then 999.6 rounds to "1000" → "1000k" — when it should
  // roll over to "1.0M". Choosing the tier from the raw value and rounding
  // afterwards is exactly what produced the inconsistent "1000k" vs "1.0M"
  // output across the dashboard. The carry loop fixes it uniformly at every
  // boundary (k→M, M→B, and the k sub-tiers) rather than special-casing one.
  let i = SCORE_TIERS.findIndex((t) => abs < t.max);
  if (i < 0) i = SCORE_TIERS.length - 1;

  let fixed = "";
  while (i < SCORE_TIERS.length) {
    const t = SCORE_TIERS[i];
    fixed = (score / t.divisor).toFixed(t.decimals);
    // Ceiling = the displayed value at which this tier "overflows" into the
    // next (e.g. 1000 for the integer-k tier whose max is 1e6 / divisor 1e3).
    const ceiling = t.max / t.divisor;
    if (Math.abs(parseFloat(fixed)) >= ceiling && i < SCORE_TIERS.length - 1) {
      i++;
      continue;
    }
    break;
  }

  const tier = SCORE_TIERS[i];
  // JS toFixed on a negative that rounds to zero produces "-0.0" / "-0.00".
  // Snap to the positive zero rendering so the SAT panel (and anything
  // else displaying near-zero scores) doesn't show a phantom minus sign.
  if (parseFloat(fixed) === 0) return (0).toFixed(tier.decimals) + tier.suffix;
  return fixed + tier.suffix;
}
