// Shared entry-point boilerplate. Every page's main.ts uses these two helpers
// instead of inlining ~17 lines of duplicated URL parsing + keyboard wiring.

export type PageId = "main" | "ideas" | "diversity" | "benchmark" | "trajectories";

const PAGES: Record<PageId, { key: string; href: string }> = {
  main:         { key: "1", href: "/" },
  ideas:        { key: "2", href: "/ideas.html" },
  diversity:    { key: "3", href: "/diversity.html" },
  benchmark:    { key: "4", href: "/benchmark.html" },
  trajectories: { key: "5", href: "/trajectories.html" },
};

// Resolves ?ws= and ?api= URL params, deriving whichever is missing.
// Defaults the WebSocket URL to /ws/dashboard on the current host; the API
// URL is derived from the WS URL by swapping protocol and stripping the path.
export function getDashboardUrls(): { wsUrl: string; apiUrl: string } {
  const params = new URLSearchParams(window.location.search);
  const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl =
    params.get("ws") ||
    `${wsProtocol}//${window.location.host}/ws/dashboard`;
  const apiUrl =
    params.get("api") ||
    wsUrl
      .replace("ws://", "http://")
      .replace("wss://", "https://")
      .replace("/ws/dashboard", "");
  return { wsUrl, apiUrl };
}

// Wires 1/2/3/4/5 to switch between dashboard pages. The current page's key
// is a no-op so pressing it doesn't trigger a reload.
export function installKeyboardNav(currentPage: PageId): void {
  document.addEventListener("keydown", (e) => {
    for (const [page, { key, href }] of Object.entries(PAGES) as [
      PageId,
      { key: string; href: string },
    ][]) {
      if (page === currentPage) continue;
      if (e.key === key) {
        window.location.href = href;
        return;
      }
    }
  });
}
