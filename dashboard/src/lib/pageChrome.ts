// Shared page-header chrome for the full-page views (ideas, diversity,
// benchmark, trajectories, leaderboard). Each page used to copy-paste the
// Prometheus mark + title + six-link nav; now they call renderPageHeader()
// (or just renderNavLinks() when the page has its own header wrapper, like
// trajectories). The emitted markup — classes and structure — must stay
// exactly as it was: style.css targets .ideas-header/.ideas-title/.ideas-nav
// and .ideas-nav-link/.ideas-nav-active directly.

import type { PageId } from "./bootstrap";

const NAV_LINKS: { page: PageId; label: string; href: string }[] = [
  { page: "main",         label: "Dashboard",    href: "/" },
  { page: "ideas",        label: "Ideas",        href: "/ideas.html" },
  { page: "diversity",    label: "Diversity",    href: "/diversity.html" },
  { page: "benchmark",    label: "Benchmark",    href: "/benchmark.html" },
  { page: "trajectories", label: "Trajectories", href: "/trajectories.html" },
  { page: "leaderboard",  label: "Leaderboard",  href: "/leaderboard.html" },
];

// The six nav entries; the active page renders as a non-link span.
export function renderNavLinks(active: PageId): string {
  return NAV_LINKS.map(({ page, label, href }) =>
    page === active
      ? `<span class="ideas-nav-active">${label}</span>`
      : `<a href="${href}" class="ideas-nav-link">${label}</a>`,
  ).join("\n        ");
}

// The standard `.ideas-header` block: mark + title on the left, nav on the
// right.
export function renderPageHeader(active: PageId, title: string): string {
  return `
    <div class="ideas-header">
      <div class="ideas-title">
        <img class="stats-mark" src="/prometheus-icon.png" alt="" draggable="false" />
        <span class="ideas-title-text">${title}</span>
      </div>
      <div class="ideas-nav">
        ${renderNavLinks(active)}
      </div>
    </div>`;
}
