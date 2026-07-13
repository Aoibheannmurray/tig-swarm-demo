<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import LogStream from "../components/LogStream.svelte";
  import { localApi } from "../lib/api";
  import { ensureStream, deployLog, deployStatus } from "../lib/stream";

  let error = $state("");
  let railway: any = $state(null);
  let challenges: any = $state({ cpu: [], gpu: [] });
  let admin: any = $state(null);

  // ── Create form ──
  let swarmType = $state("cpu");
  let swarmName = $state("my-tig-swarm");
  let workspace = $state("");
  let activeChallenge = $state("");
  let stagThreshold = $state(2);
  let stagLimit = $state(4);
  let recallThreshold = $state(3);
  let seedInactive = $state(false);
  let useDefaults = $state(true);
  // Per-challenge {track_key: instances} edits for the customize view,
  // seeded from the server's track_defaults (same values the CLI wizard uses).
  let trackEdits: Record<string, Record<string, number>> = $state({});
  let deploying = $state(false);

  let challengeList = $derived(swarmType === "gpu" ? challenges.gpu : challenges.cpu);
  $effect(() => {
    if (challengeList.length && !challengeList.includes(activeChallenge)) {
      activeChallenge = challengeList[0];
    }
  });

  // ── Manage existing ──
  let switchTo = $state("");
  let switchMsg = $state("");

  onMount(async () => {
    try {
      await refreshRailway();
      challenges = await localApi.challenges();
      const defaults = challenges.track_defaults ?? {};
      const edits: Record<string, Record<string, number>> = {};
      for (const c of challenges.all ?? []) edits[c] = { ...(defaults[c] ?? {}) };
      trackEdits = edits;
      admin = await localApi.swarmAdmin();
      if (admin?.active_challenge) switchTo = admin.active_challenge;
      // Recover a deploy that's still running (or that finished) from a prior
      // page — e.g. a reload mid-provision — so the UI re-attaches instead of
      // showing an idle create form over a live deploy.
      const s = await localApi.swarmCreateStatus();
      if (s.running) {
        deploying = true;
      } else if (s.state === "done" || s.state === "error") {
        deployStatus.set({ state: s.state, result: s.result, error: s.error });
      }
    } catch (e: any) {
      error = e.message;
    }
  });

  async function refreshRailway() {
    railway = await localApi.railwayStatus();
    const ws: string[] = railway?.workspaces ?? [];
    if (ws.length && !ws.includes(workspace)) workspace = ws[0];
  }

  async function recheckRailway() {
    error = "";
    try {
      await refreshRailway();
    } catch (e: any) {
      error = e.message;
    }
  }

  // ── Login from the UI (device-code flow) ──
  // POST spawns `railway login --browserless` on the companion; we show the
  // pairing link + code it prints and poll until the CLI exits (paired) or
  // fails (codes expire after a few minutes — the button restarts cleanly).
  let login: any = $state(null);
  let loginPoll: ReturnType<typeof setInterval> | null = null;
  function stopLoginPoll() {
    if (loginPoll) { clearInterval(loginPoll); loginPoll = null; }
  }
  async function startLogin() {
    error = "";
    try {
      login = await localApi.railwayLoginStart();
    } catch (e: any) {
      error = e.message;
      return;
    }
    stopLoginPoll();
    loginPoll = setInterval(async () => {
      try {
        login = await localApi.railwayLoginStatus();
        if (login.state === "done") {
          stopLoginPoll();
          await refreshRailway();
          if (railway?.authed) login = null;
        } else if (login.state === "error") {
          stopLoginPoll();
        }
      } catch { /* companion hiccup — keep polling */ }
    }, 2000);
  }
  onDestroy(stopLoginPoll);

  async function createSwarm() {
    error = ""; deploying = true;
    try {
      ensureStream();
      const payload: any = {
        swarm_type: swarmType,
        swarm_name: swarmName,
        active_challenge: activeChallenge,
        stagnation_threshold: stagThreshold,
        stagnation_limit: stagLimit,
        hypothesis_recall_threshold: recallThreshold,
        seed_inactive_pool: seedInactive,
      };
      if (workspace) payload.workspace = workspace;
      if (!useDefaults) {
        payload.tracks = Object.fromEntries(
          challengeList.map((c: string) => [c, trackEdits[c] ?? {}]),
        );
      }
      await localApi.swarmCreate(payload);
    } catch (e: any) {
      error = e.message;
      deploying = false;
    }
  }

  $effect(() => {
    if ($deployStatus.state === "done" || $deployStatus.state === "error") deploying = false;
  });

  // Fallback poll: the deploy's completion is normally delivered as a single
  // `deploy_status` event over the WebSocket, but that socket sits idle for the
  // minutes-long Railway build (build logs stream to the terminal, not the hub),
  // so a silently-dropped connection can swallow the final event and strand the
  // UI on "Provisioning…" even though the backend finished. While a deploy is in
  // flight, poll the authoritative status endpoint and reconcile on a terminal
  // state — the $effect above then clears `deploying` and renders the result.
  $effect(() => {
    if (!deploying) return;
    const timer = setInterval(async () => {
      try {
        const s = await localApi.swarmCreateStatus();
        if (s.state === "done" || s.state === "error") {
          deployStatus.set({ state: s.state, result: s.result, error: s.error });
        }
      } catch {
        /* transient companion hiccup — keep polling */
      }
    }, 3000);
    return () => clearInterval(timer);
  });

  async function doSwitch() {
    switchMsg = ""; error = "";
    try {
      const r = await localApi.swarmSwitch(switchTo);
      switchMsg = `Active challenge → ${r.active_challenge}`;
      admin = await localApi.swarmAdmin();
    } catch (e: any) {
      error = e.message;
    }
  }

  function adminConsoleUrl(): string {
    // The Admin Console is served by the swarm's own server at /admin/.
    const base = ($deployStatus.result?.server_url || admin?.server_url || "").replace(/\/$/, "");
    return base ? `${base}/admin/` : "/admin/";
  }
</script>

{#if error}<div class="banner err">{error}</div>{/if}

<!-- Railway status -->
<div class="card">
  <div class="rowhead">
    <h2>Railway</h2>
    <div class="spacer"></div>
    {#if railway?.authed}
      <span class="pill ok">authed · {railway.user}</span>
    {:else}
      <span class="pill warn">not connected</span>
      <button onclick={recheckRailway}>Recheck</button>
    {/if}
  </div>
  {#if !railway?.authed}
    {#if !login || login.state === "idle"}
      <p class="lede">
        Provisioning needs the Railway CLI, logged in.
        <b>Log in right here</b> — or run <code>railway login</code> in a
        terminal and hit Recheck.
        {#if railway?.message}<br /><span class="muted mono">{railway.message}</span>{/if}
      </p>
      <button class="primary" onclick={startLogin}>Log in to Railway</button>
    {:else if login.state === "pending"}
      {#if login.url}
        <p class="lede">
          Open this link in any browser
          {#if login.code} and enter code <b class="mono">{login.code}</b>{/if}
          — this page updates by itself once you're signed in.
        </p>
        <div class="cmd mono" style="word-break:break-all;margin-bottom:8px">
          <a href={login.url} target="_blank" rel="noopener">{login.url}</a>
        </div>
      {:else}
        <p class="lede">Starting the Railway sign-in…</p>
        {#if login.output}<pre class="mono muted" style="white-space:pre-wrap">{login.output}</pre>{/if}
      {/if}
      <button class="ghost" onclick={startLogin}>Start over</button>
    {:else if login.state === "error"}
      <div class="banner err">{login.error || "Login failed."}</div>
      {#if login.output}<pre class="mono muted" style="white-space:pre-wrap">{login.output}</pre>{/if}
      <button class="primary" onclick={startLogin}>Try again</button>
    {:else if login.state === "done"}
      <p class="lede">Signed in — refreshing status…</p>
    {/if}
  {/if}
</div>

<!-- Create -->
<div class="card">
  <h2>Create a swarm</h2>
  <p class="lede">Stand up a new coordination server on Railway. It runs 24/7 independently of this machine.</p>
  <div class="row">
    <div class="field">
      <label for="type">Type</label>
      <select id="type" bind:value={swarmType}>
        <option value="cpu">CPU swarm</option>
        <option value="gpu">GPU swarm</option>
      </select>
    </div>
    <div class="field">
      <label for="name">Swarm name</label>
      <input id="name" type="text" bind:value={swarmName} placeholder="my-tig-swarm" />
    </div>
  </div>
  {#if (railway?.workspaces?.length ?? 0) > 1}
    <div class="field">
      <label for="ws">Railway workspace</label>
      <select id="ws" bind:value={workspace}>
        {#each railway.workspaces as w}<option value={w}>{w}</option>{/each}
      </select>
      <div class="hint">Your Railway account has multiple workspaces — the new project is created in this one.</div>
    </div>
  {/if}
  <div class="field">
    <label for="ach">Active challenge</label>
    <select id="ach" bind:value={activeChallenge}>
      {#each challengeList as c}<option value={c}>{c}</option>{/each}
    </select>
    <div class="hint">Contributors auto-follow this. Switch any time later; per-challenge state is preserved.</div>
  </div>
  <div class="row">
    <div class="field">
      <label for="st">Stagnation threshold</label>
      <input id="st" type="number" min="1" bind:value={stagThreshold} />
    </div>
    <div class="field">
      <label for="sl">Stagnation limit</label>
      <input id="sl" type="number" min="0" bind:value={stagLimit} />
    </div>
    <div class="field">
      <label for="rt">Recall threshold</label>
      <input id="rt" type="number" min="1" bind:value={recallThreshold} />
    </div>
  </div>
  <div class="field">
    <label class="check"><input type="checkbox" bind:checked={seedInactive} /> Seed the inactive pool from the top TIG mainnet algorithm</label>
  </div>
  <div class="field">
    <label class="check"><input type="checkbox" bind:checked={useDefaults} /> Use recommended benchmark instance counts for every challenge</label>
  </div>
  {#if !useDefaults}
    <div class="tracks">
      {#each challengeList as c}
        <div class="trackgroup">
          <div class="trackname">{c}</div>
          {#each Object.keys(trackEdits[c] ?? {}) as key}
            <div class="trackrow">
              <span class="mono">{key}</span>
              <input type="number" min="0" bind:value={trackEdits[c][key]} />
            </div>
          {/each}
        </div>
      {/each}
      <div class="hint">Benchmark instances per track for each challenge. 0 disables a track.</div>
    </div>
  {/if}
  <div class="actions">
    <div class="spacer"></div>
    <button class="primary" disabled={deploying || !railway?.authed} onclick={createSwarm}>
      {deploying ? "Provisioning…" : "Provision on Railway"}
    </button>
  </div>
</div>

<!-- Deploy progress -->
{#if $deployLog.length || deploying || $deployStatus.state !== "idle"}
  <div class="card">
    <div class="rowhead">
      <h2>Deploy progress</h2>
      <div class="spacer"></div>
      <span class="pill {$deployStatus.state === 'done' ? 'ok' : $deployStatus.state === 'error' ? 'err' : 'info'}">{$deployStatus.state}</span>
    </div>
    <LogStream lines={$deployLog} height="300px" />
    {#if $deployStatus.state === "error" && $deployStatus.error}
      <div class="banner err" style="margin-top:16px">{$deployStatus.error}</div>
    {/if}
    {#if $deployStatus.state === "done" && $deployStatus.result}
      {@const r = $deployStatus.result}
      <div class="banner ok" style="margin-top:16px">
        {r.type_label} swarm is live at <a href={r.server_url} target="_blank" rel="noreferrer">{r.server_url}</a>
      </div>
      <ul class="creds">
        <li><span>Dashboard</span><a href={`${r.server_url}/`} target="_blank" rel="noreferrer">{r.server_url}/</a></li>
        <li><span>Admin key</span><code>{r.admin_key}</code></li>
        <li><span>Base password</span><code>{r.swarm_password}</code></li>
      </ul>
      <p class="lede" style="margin-top:14px">
        <b>Next:</b> open the Admin Console to create a <b>join link</b> for each
        contributor — they configure agents in the browser, then run one command
        (<span class="mono">python run.py --join "&lt;link&gt;"</span>) or a no-clone
        path from the README.
      </p>
      <div class="actions">
        <div class="spacer"></div>
        <a class="btn primary" href={adminConsoleUrl()} target="_blank" rel="noreferrer">Open Admin Console →</a>
      </div>
    {/if}
  </div>
{/if}

<!-- Manage existing -->
{#if admin?.admin_key}
  <div class="card">
    <h2>Manage this swarm</h2>
    <ul class="creds">
      <li><span>Server</span><a href={admin.server_url} target="_blank" rel="noreferrer">{admin.server_url}</a></li>
      <li><span>Active challenge</span><b>{admin.active_challenge ?? "—"}</b></li>
    </ul>
    <div class="row" style="align-items:flex-end">
      <div class="field" style="margin-bottom:0">
        <label for="sw">Switch active challenge</label>
        <select id="sw" bind:value={switchTo}>
          {#each challenges.all ?? [...challenges.cpu, ...challenges.gpu] as c}<option value={c}>{c}</option>{/each}
        </select>
      </div>
      <div style="flex:0 0 auto"><button onclick={doSwitch}>Switch</button></div>
    </div>
    {#if switchMsg}<div class="banner ok" style="margin-top:14px">{switchMsg}</div>{/if}
    <div class="actions">
      <div class="spacer"></div>
      <a class="btn" href={adminConsoleUrl()} target="_blank" rel="noreferrer">Open Admin Console →</a>
    </div>
  </div>
{/if}

<style>
  .rowhead { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .cmd {
    background: var(--bg-sunken, rgba(127, 127, 127, 0.12));
    border-radius: 6px;
    padding: 8px 10px;
    overflow-x: auto;
    font-size: 0.92em;
  }
  .rowhead h2 { margin: 0; }
  .check { display: flex; align-items: center; gap: 8px; text-transform: none; letter-spacing: 0; font-weight: 500; color: var(--ink); }
  .check input { width: auto; }
  .creds { list-style: none; margin: 6px 0 4px; }
  .creds li { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0; border-bottom: 1px solid var(--border-subtle); font-size: 14px; }
  .creds li span { color: var(--ink-dim); }
  .tracks { margin: 4px 0 14px; }
  .trackgroup { padding: 8px 0; border-bottom: 1px solid var(--border-subtle); }
  .trackname { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  .trackrow { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 3px 0; font-size: 13px; }
  .trackrow input { width: 90px; flex: 0 0 auto; }
  .tracks .hint { font-size: 12.5px; color: var(--ink-dim); margin-top: 8px; }
</style>
