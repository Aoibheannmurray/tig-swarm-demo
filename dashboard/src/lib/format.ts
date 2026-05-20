// Compact score formatting: 4879698.2 → "4.88M", 157094.9 → "157k".
// Used in the leaderboard, chart axes, feed and strategy leaderboard so
// large baseline-relative quality scores stay readable at a glance.

export function formatScore(score: number | null | undefined): string {
  if (score == null || Number.isNaN(score)) return "—";
  const abs = Math.abs(score);

  // Pick value/decimals/suffix by magnitude bucket, then format once at
  // the bottom. Computing the rounded string before deciding the sign
  // lets us normalise "-0.0" → "0.0" (small negative scores like -0.04
  // would otherwise round to a misleading "-0.0").
  let value: number;
  let decimals: number;
  let suffix = "";

  if (abs < 1000) {
    // Small magnitudes — keep one decimal so the value isn't rounded to 0.
    value = score;
    decimals = 1;
  } else if (abs < 1e4) {
    // 1k–9.99k.
    value = score / 1000;
    decimals = 2;
    suffix = "k";
  } else if (abs < 1e5) {
    // 10k–99.9k.
    value = score / 1000;
    decimals = 1;
    suffix = "k";
  } else if (abs < 1e6) {
    // 100k–999k — integer k so wider numbers don't visually crowd
    // narrow leaderboard cells.
    value = score / 1000;
    decimals = 0;
    suffix = "k";
  } else if (abs < 1e9) {
    value = score / 1e6;
    decimals = 2;
    suffix = "M";
  } else {
    value = score / 1e9;
    decimals = 2;
    suffix = "B";
  }

  const fixed = value.toFixed(decimals);
  // JS toFixed on a negative that rounds to zero produces "-0.0" / "-0.00".
  // Snap to the positive zero rendering so the SAT panel (and anything
  // else displaying near-zero scores) doesn't show a phantom minus sign.
  if (parseFloat(fixed) === 0) return (0).toFixed(decimals) + suffix;
  return fixed + suffix;
}
