// Shared helpers for the two trajectory heatmap panels (panels/diversity.ts
// and pages/diversity/inspiration-matrix.ts). Both render the same `.dv-grid`
// DOM and refresh on leaderboard updates with the same 30s throttle; only the
// data source and cell coloring differ, so those stay in each panel.

// The server labels rows as "<traj-id> · <agent-name>(possibly · inactive)".
// The traj-id prefix is what the operator scans for, so keep it intact and
// truncate from the trailing agent-name half when the label is too long for
// the heatmap chip.
export function heatmapShortName(name: string): string {
  if (name.length <= 12) return name;
  const dot = " · ";
  const idx = name.indexOf(dot);
  if (idx < 0 || idx >= 10) return name.slice(0, 11) + "…";
  const head = name.slice(0, idx); // traj-id
  const tail = name.slice(idx + dot.length);
  const tailBudget = Math.max(1, 12 - head.length - dot.length);
  return tail.length <= tailBudget
    ? `${head}${dot}${tail}`
    : `${head}${dot}${tail.slice(0, tailBudget)}…`;
}

// The empty top-left cell of the heatmap grid.
export function heatmapCorner(): HTMLElement {
  const el = document.createElement("div");
  el.className = "dv-corner";
  return el;
}

// Rate-limits a refresh callback: runs immediately when the interval has
// elapsed since the last run, otherwise defers a single run to the interval
// boundary (coalescing any requests that arrive while one is pending).
export class ThrottledRefresh {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private lastRun = 0;
  private readonly run: () => void;
  private readonly intervalMs: number;

  constructor(run: () => void, intervalMs = 30_000) {
    this.run = run;
    this.intervalMs = intervalMs;
  }

  // Record that a run just started. Call from the refresh function itself so
  // direct invocations (initial load, challenge switch) also arm the window.
  markRun(): void {
    this.lastRun = Date.now();
  }

  // Zero the window so the next request() (or the panel's own direct call)
  // is not considered throttled.
  forceNext(): void {
    this.lastRun = 0;
  }

  // Cancel a pending deferred run, if any.
  cancel(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  // Run now if the interval has elapsed, else defer one run to the boundary.
  request(): void {
    const elapsed = Date.now() - this.lastRun;
    if (elapsed >= this.intervalMs) {
      this.run();
    } else if (!this.timer) {
      this.timer = setTimeout(() => {
        this.timer = null;
        this.run();
      }, this.intervalMs - elapsed);
    }
  }
}
