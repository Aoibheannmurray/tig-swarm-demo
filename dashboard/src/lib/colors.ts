// Earthen-extended palette — 8 base hues from the Prometheus design system,
// 8 lighter variants, 4 darker accents. Order is chosen so adjacent slots
// stay visually distinguishable (FNV-1a hashing distributes agents across
// the full range, but rank-ordered displays should still read cleanly).
export const PALETTE = [
  // 8 base earthen hues (mirror of --viz-1 .. --viz-8)
  "#B8541F", "#C68F3E", "#6B7F4E", "#4E6B85",
  "#7A4F6E", "#4A8C8A", "#A66E45", "#8B6B8C",
  // 8 lighter variants
  "#D9794A", "#E0BE6E", "#A4B26A", "#8FA8C2",
  "#A88AB6", "#92BABA", "#C49074", "#B8A0BA",
  // 4 darker hue-shifted accents
  "#7A3812", "#8E6A14", "#4D5A26", "#3F5A78",
];

export const ROUTE_COLORS = PALETTE.slice(0, 10);

const agentColorMap = new Map<string, string>();

// Neutral fallback used when no agent_id is associated with an event (admin
// broadcasts, ungoverned system messages). Kept here so every panel shares
// the same "no-agent" color instead of inventing its own.
export const NEUTRAL_AGENT_COLOR = "var(--text-dim)";

// Assign and cache an agent's color. The agent's *preferred* slot is the
// FNV-1a hash of its id mod palette size, which keeps colors stable across
// reloads in the common case. If the preferred slot is already claimed we
// walk forward through the palette and take the first free slot — uniqueness
// is guaranteed for the first PALETTE.length agents. Beyond that we accept
// the hashed collision.
//
// Called by main.ts on agent_joined / leaderboard_update so every agent's
// color is pinned at registration time, before any feed item or chart point
// looks it up. Idempotent: re-registering an agent returns the cached color.
export function registerAgentColor(agentId: string): string {
  const cached = agentColorMap.get(agentId);
  if (cached) return cached;

  // FNV-1a 32-bit
  let h = 0x811c9dc5;
  for (let i = 0; i < agentId.length; i++) {
    h ^= agentId.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) | 0;
  }
  const preferred = Math.abs(h) % PALETTE.length;

  const used = new Set(agentColorMap.values());
  let color = PALETTE[preferred];
  if (used.size < PALETTE.length) {
    for (let i = 0; i < PALETTE.length; i++) {
      const slot = (preferred + i) % PALETTE.length;
      if (!used.has(PALETTE[slot])) {
        color = PALETTE[slot];
        break;
      }
    }
  }
  agentColorMap.set(agentId, color);
  return color;
}

// Resolve an agent_id to its palette color. Falls back to registerAgentColor
// so panels that observe an agent before main.ts gets a chance to register it
// still produce a stable, unique slot. Every panel routes through this so the
// leaderboard dot, chart line, diversity row, and feed item all paint the
// same agent the same color.
export function getAgentColor(agentId: string): string {
  return registerAgentColor(agentId);
}

export function getRouteColor(vehicleIndex: number): string {
  return ROUTE_COLORS[vehicleIndex % ROUTE_COLORS.length];
}

// Resolve a CSS custom property to a concrete color string for use in D3
// `.attr("fill", ...)` etc., where var() references aren't accepted. Cached
// after first read; the dashboard never live-toggles tokens at runtime.
const tokenCache = new Map<string, string>();
export function token(name: string, fallback: string): string {
  const cached = tokenCache.get(name);
  if (cached) return cached;
  if (typeof document === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  const value = v || fallback;
  tokenCache.set(name, value);
  return value;
}
