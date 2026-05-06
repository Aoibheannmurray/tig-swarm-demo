// Compact score formatting: 4879698.2 → "4.88M", 157094.9 → "157k".
// Used in the leaderboard, chart axes, feed and strategy leaderboard so
// large baseline-relative quality scores stay readable at a glance.

export function formatScore(score: number | null | undefined): string {
  if (score == null || Number.isNaN(score)) return "—";
  const abs = Math.abs(score);
  const sign = score < 0 ? "-" : "";

  if (abs < 1000) {
    // Small magnitudes — keep one decimal so the value isn't rounded to 0.
    return sign + abs.toFixed(1);
  }
  if (abs < 1e6) {
    // 1k–999k. Two decimals under 10k, one between 10k and 100k, integer k
    // above so wider numbers don't visually crowd narrow leaderboard cells.
    if (abs < 1e4) return sign + (abs / 1000).toFixed(2) + "k";
    if (abs < 1e5) return sign + (abs / 1000).toFixed(1) + "k";
    return sign + Math.round(abs / 1000) + "k";
  }
  if (abs < 1e9) {
    return sign + (abs / 1e6).toFixed(2) + "M";
  }
  return sign + (abs / 1e9).toFixed(2) + "B";
}
