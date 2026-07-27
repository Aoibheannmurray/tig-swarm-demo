// API clients for the two surfaces.
//
//  - localApi:  talks to the local `control_server.py` companion (/local-api/*)
//               for host provisioning + contributor fleet control.
//  - hostedApi: talks to a swarm's hosted FastAPI server (/api/*, /api/admin/*)
//               for the Admin Console.

async function jsonOrThrow(res: Response): Promise<any> {
  let body: any = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON error body */
  }
  if (!res.ok) {
    const msg = (body && (body.error || body.detail)) || `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return body;
}

// ── Local companion ──────────────────────────────────────────────────

const LOCAL = "/local-api";

export const localApi = {
  env: () => fetch(`${LOCAL}/env`).then(jsonOrThrow),
  preflight: () => fetch(`${LOCAL}/preflight`).then(jsonOrThrow),
  providers: () => fetch(`${LOCAL}/providers`).then(jsonOrThrow),
  challenges: () => fetch(`${LOCAL}/challenges`).then(jsonOrThrow),
  // Live model catalog for one provider (including the installed Codex CLI).
  // Never throws for "no key" / unsupported CLI provider — those come back as
  // {models: [], error} so the caller can use the curated fallback.
  // `custom` sends its own endpoint along: that catalog belongs to the
  // contributor's server, not to a provider we ship a table for.
  models: (provider: string, refresh = false, endpoint?: { api_base?: string; api_key_env?: string }) =>
    fetch(`${LOCAL}/models?provider=${encodeURIComponent(provider)}` +
      (refresh ? "&refresh=true" : "") +
      (endpoint?.api_base ? `&api_base=${encodeURIComponent(endpoint.api_base)}` : "") +
      (endpoint?.api_key_env ? `&api_key_env=${encodeURIComponent(endpoint.api_key_env)}` : "")
    ).then(jsonOrThrow),

  getFleetConfig: () => fetch(`${LOCAL}/fleet/config`).then(jsonOrThrow),
  setFleetConfig: (params: any) =>
    fetch(`${LOCAL}/fleet/config`, post(params)).then(jsonOrThrow),
  saveFleetConfig: (config: any) =>
    fetch(`${LOCAL}/fleet/config/save`, post({ config })).then(jsonOrThrow),
  // The same prompts `python setup.py tacit` asks — served, not duplicated in
  // the UI, so the two interviews can't drift apart.
  tacitQuestions: () => fetch(`${LOCAL}/tacit/questions`).then(jsonOrThrow),
  setTacit: (payload: any) =>
    fetch(`${LOCAL}/tacit`, post(payload)).then(jsonOrThrow),

  fleetStatus: () => fetch(`${LOCAL}/fleet/status`).then(jsonOrThrow),
  fleetStart: (only?: string[]) =>
    fetch(`${LOCAL}/fleet/start`, post({ only })).then(jsonOrThrow),
  fleetStop: () => fetch(`${LOCAL}/fleet/stop`, post({})).then(jsonOrThrow),

  secretsStatus: () => fetch(`${LOCAL}/secrets`).then(jsonOrThrow),
  secretSet: (name: string, value: string) =>
    fetch(`${LOCAL}/secrets`, post({ name, value })).then(jsonOrThrow),

  railwayStatus: () => fetch(`${LOCAL}/railway/status`).then(jsonOrThrow),
  railwayNameCheck: (name: string, workspace?: string) =>
    fetch(`${LOCAL}/railway/name-check?name=${encodeURIComponent(name)}` +
      (workspace ? `&workspace=${encodeURIComponent(workspace)}` : "")).then(jsonOrThrow),
  railwayLoginStart: () => fetch(`${LOCAL}/railway/login`, { method: "POST" }).then(jsonOrThrow),
  railwayLoginStatus: () => fetch(`${LOCAL}/railway/login`).then(jsonOrThrow),
  railwayInstallStart: () => fetch(`${LOCAL}/railway/install`, { method: "POST" }).then(jsonOrThrow),
  railwayInstallStatus: () => fetch(`${LOCAL}/railway/install`).then(jsonOrThrow),
  dockerInstallStart: () => fetch(`${LOCAL}/docker/install`, { method: "POST" }).then(jsonOrThrow),
  dockerInstallStatus: () => fetch(`${LOCAL}/docker/install`).then(jsonOrThrow),
  // Install AND update share one endpoint: re-running overwrites the binary,
  // which is what "update c3" means.
  c3InstallStart: () => fetch(`${LOCAL}/c3/install`, { method: "POST" }).then(jsonOrThrow),
  c3InstallStatus: () => fetch(`${LOCAL}/c3/install`).then(jsonOrThrow),
  swarmAdmin: () => fetch(`${LOCAL}/swarm/admin`).then(jsonOrThrow),
  // Derive a contributor's credentials from the host's base password, and
  // record the name in issued_contributors. Lets a host join their own swarm
  // without the Admin Console → join link → browser detour.
  invite: (username: string) =>
    fetch(`${LOCAL}/invite`, post({ username })).then(jsonOrThrow),
  swarmCreate: (params: any) =>
    fetch(`${LOCAL}/swarm/create`, post(params)).then(jsonOrThrow),
  // Authoritative deploy state, independent of the WebSocket. Polled as a
  // fallback so a dropped `deploy_status` event can't leave the UI stuck on
  // "Provisioning…" forever (the Railway build is minutes of WS silence).
  swarmCreateStatus: () =>
    fetch(`${LOCAL}/swarm/create/status`).then(jsonOrThrow),
  swarmSwitch: (challenge: string) =>
    fetch(`${LOCAL}/swarm/switch`, post({ challenge })).then(jsonOrThrow),
  seedStatus: () => fetch(`${LOCAL}/swarm/seed_status`).then(jsonOrThrow),
  reseed: (mainnet = false) =>
    fetch(`${LOCAL}/swarm/reseed`, post({ mainnet })).then(jsonOrThrow),
};

// Live event stream from the companion (fleet logs + deploy progress).
export function connectStream(onEvent: (ev: any) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${LOCAL}/stream`);
  ws.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data));
    } catch {
      /* ignore malformed */
    }
  };
  return ws;
}

// ── Hosted server (Admin Console) ────────────────────────────────────
//
// The admin console is served BY the swarm's own server, so same-origin
// `/api/*` calls hit that server. Admin endpoints take the admin_key in the
// POST body. In dev you can override the base via ?server=<url>.

export function hostedBase(): string {
  const q = new URLSearchParams(location.search).get("server");
  return q ? q.replace(/\/$/, "") : "";
}

function contribHeaders(username: string, password: string): Record<string, string> {
  return { "X-Username": username, "X-Swarm-Password": password };
}

export const hostedApi = {
  swarmConfig: () => fetch(`${hostedBase()}/api/swarm_config`).then(jsonOrThrow),

  // Contributor-credential-gated (X-Username / X-Swarm-Password — the same
  // pair that gates agent registration). Used by the hosted /join page to
  // validate an invite and describe the swarm. (The wider /api/contributor/*
  // config endpoints still exist server-side for headless runs, but the UI
  // no longer surfaces them — config lives in the LOCAL setup app.)
  contributorMe: (username: string, password: string) =>
    fetch(`${hostedBase()}/api/contributor/me`, {
      headers: contribHeaders(username, password),
    }).then(jsonOrThrow),

  // admin_key-gated POSTs
  contributors: (adminKey: string) =>
    fetch(`${hostedBase()}/api/admin/contributors`, post({ admin_key: adminKey })).then(jsonOrThrow),
  revoke: (adminKey: string, username: string) =>
    fetch(`${hostedBase()}/api/admin/revoke`, post({ admin_key: adminKey, username })).then(jsonOrThrow),
  broadcast: (adminKey: string, message: string, priority = "normal") =>
    fetch(`${hostedBase()}/api/admin/broadcast`, post({ admin_key: adminKey, message, priority })).then(jsonOrThrow),
  setActiveChallenge: (adminKey: string, challenge: string) =>
    fetch(`${hostedBase()}/api/swarm_config`, post({ admin_key: adminKey, active_challenge: challenge })).then(jsonOrThrow),
  updateSwarmConfig: (adminKey: string, payload: Record<string, any>) =>
    fetch(`${hostedBase()}/api/swarm_config`, post({ admin_key: adminKey, ...payload })).then(jsonOrThrow),
  seedInactive: (adminKey: string, challenge: string) =>
    fetch(`${hostedBase()}/api/admin/seed_inactive`, post({ admin_key: adminKey, challenge })).then(jsonOrThrow),
  listSeeds: (adminKey: string, challenge: string) =>
    fetch(`${hostedBase()}/api/admin/seeds`, post({ admin_key: adminKey, challenge })).then(jsonOrThrow),
  seedFromMainnet: (adminKey: string, challenge: string | null, target: string) =>
    fetch(`${hostedBase()}/api/admin/seed_from_mainnet`,
      post({ admin_key: adminKey, challenge, target })).then(jsonOrThrow),
  clearInactive: (adminKey: string, challenge: string) =>
    fetch(`${hostedBase()}/api/admin/clear_inactive`, post({ admin_key: adminKey, challenge })).then(jsonOrThrow),
  resetChallenge: (adminKey: string, challenge: string) =>
    fetch(`${hostedBase()}/api/admin/reset_challenge`, post({ admin_key: adminKey, challenge })).then(jsonOrThrow),
};

// One-link form of an invite: the swarm server's /join page with the
// credentials in the URL *fragment*, which browsers never send to the server
// — they stay out of proxy/host logs. Mirrors
// hostadmin/contributors.py:build_join_link; keep the two in sync.
export function buildJoinLink(serverUrl: string, username: string, password: string): string {
  const base = serverUrl.replace(/\/$/, "");
  return `${base}/join#u=${encodeURIComponent(username)}&p=${encodeURIComponent(password)}`;
}

// Derive a contributor's per-swarm password client-side (mirrors setup.py
// run_invite: sha256(username + ':' + base)). Used by the Admin Console so the
// host can generate invites without any server round-trip.
export async function deriveInvitePassword(username: string, base: string): Promise<string> {
  // crypto.subtle only exists in secure contexts — on a plain-HTTP,
  // non-localhost origin it's undefined and the digest below would throw
  // an opaque TypeError. Fail with an actionable message instead.
  if (!crypto?.subtle) {
    throw new Error("Invite generation needs HTTPS or localhost (Web Crypto unavailable)");
  }
  const data = new TextEncoder().encode(`${username}:${base}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function post(body: any): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  };
}
