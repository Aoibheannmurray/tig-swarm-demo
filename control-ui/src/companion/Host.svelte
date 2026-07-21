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
  let seedPoolMainnet = $state(false);
  let useDefaults = $state(true);
  // Per-challenge {track_key: instances} edits for the customize view,
  // seeded from the server's track_defaults (same values the CLI wizard uses).
  let trackEdits: Record<string, Record<string, number>> = $state({});
  // Per-challenge solver timeout (seconds) for the customize view, seeded
  // from the registry defaults. Hot-editable later in the Admin Console.
  let timeoutEdits: Record<string, number> = $state({});
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

  // ── Seed pool ──
  // The pool lives only in the swarm's DB and is written only by create's
  // authored-seed deposit; a server DB reset (e.g. redeploy onto a
  // non-persistent volume) empties it, and agents fall back to the bare stub.
  let seed: any = $state(null);
  let seedMsg = $state("");
  let reseeding = $state(false);
  let emptyPool = $derived((seed?.empty ?? []).length > 0);

  async function refreshSeed() {
    try { seed = await localApi.seedStatus(); } catch { seed = null; }
  }

  let reseedMainnet = $state(false);
  async function doReseed() {
    reseeding = true; seedMsg = ""; error = "";
    try {
      const r = await localApi.reseed(reseedMainnet);
      let msg = `Re-seeded ${r.deposited}/${r.total} authored seed(s)` +
        (r.missing?.length ? ` — still missing: ${r.missing.join(", ")}` : " — pool verified.");
      if (r.mainnet) {
        msg += r.mainnet_failed?.length
          ? ` Mainnet: failed for ${r.mainnet_failed.join(", ")}.`
          : " Mainnet algorithm deposited.";
      }
      seedMsg = msg;
      await refreshSeed();
    } catch (e: any) {
      error = e.message;
    } finally {
      reseeding = false;
    }
  }

  onMount(async () => {
    try {
      await refreshRailway();
      challenges = await localApi.challenges();
      const defaults = challenges.track_defaults ?? {};
      const edits: Record<string, Record<string, number>> = {};
      for (const c of challenges.all ?? []) edits[c] = { ...(defaults[c] ?? {}) };
      trackEdits = edits;
      const tdefaults = challenges.timeout_defaults ?? {};
      timeoutEdits = Object.fromEntries(
        (challenges.all ?? []).map((c: string) => [c, tdefaults[c] ?? 30]),
      );
      admin = await localApi.swarmAdmin();
      if (admin?.active_challenge) switchTo = admin.active_challenge;
      if (admin?.admin_key) refreshSeed();
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

  // ── Install the Railway CLI from the UI ──
  // POST runs the vendor installer (railway.com/install.sh) on the companion;
  // we poll until it exits, then re-read status (now installed → login flow).
  let install: any = $state(null);
  let installPoll: ReturnType<typeof setInterval> | null = null;
  function stopInstallPoll() {
    if (installPoll) { clearInterval(installPoll); installPoll = null; }
  }
  async function startInstall() {
    error = "";
    try {
      install = await localApi.railwayInstallStart();
    } catch (e: any) {
      error = e.message;
      return;
    }
    stopInstallPoll();
    installPoll = setInterval(async () => {
      try {
        install = await localApi.railwayInstallStatus();
        if (install.state === "done") {
          stopInstallPoll();
          await refreshRailway();
          if (railway?.installed) install = null;
        } else if (install.state === "error") {
          stopInstallPoll();
        }
      } catch { /* companion hiccup — keep polling */ }
    }, 2000);
  }
  onDestroy(stopInstallPoll);

  // Copy a credential to the clipboard for pasting into the Admin Console.
  // Keyed by field so only the clicked row flips to "Copied".
  let copied = $state("");
  async function copyText(field: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      copied = field;
      setTimeout(() => { if (copied === field) copied = ""; }, 1500);
    } catch { /* clipboard blocked (non-secure context) — leave the value visible */ }
  }

  // ── Adopt confirmation ──
  // Provisioning onto an existing name ADOPTS that swarm (data volume and
  // credentials are preserved) — but it redeploys a live server, so it must
  // never happen by accident. Set while the host confirms; cleared either way.
  let adoptPrompt: any = $state(null);
  let checkingName = $state(false);

  async function createSwarm() {
    error = "";
    // Pre-flight: warn once if the name is taken. `adoptPrompt` being set means
    // the host already saw the warning and clicked through.
    if (!adoptPrompt) {
      checkingName = true;
      try {
        const check = await localApi.railwayNameCheck(swarmName, workspace);
        if (check.exists) {
          adoptPrompt = check;
          checkingName = false;
          return;
        }
      } catch {
        // Name check is advisory — a companion/Railway hiccup must not block
        // a legitimate create.
      }
      checkingName = false;
    }
    adoptPrompt = null;
    deploying = true;
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
        seed_pool_mainnet: seedPoolMainnet,
      };
      if (workspace) payload.workspace = workspace;
      if (!useDefaults) {
        payload.tracks = Object.fromEntries(
          challengeList.map((c: string) => [c, trackEdits[c] ?? {}]),
        );
        payload.timeouts = Object.fromEntries(
          challengeList.map((c: string) => [c, timeoutEdits[c] ?? 30]),
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
    {:else if railway && !railway.installed}
      <span class="pill warn">CLI not installed</span>
      <button onclick={recheckRailway}>Recheck</button>
    {:else}
      <span class="pill warn">not connected</span>
      <button onclick={recheckRailway}>Recheck</button>
    {/if}
  </div>

  {#if railway && !railway.installed}
    <!-- No CLI: offer to install it (provisioning shells out to `railway`). -->
    {#if !install || install.state === "idle"}
      <p class="lede">
        Provisioning runs on the Railway CLI, which isn't installed yet.
        <b>Install it here</b> — or run
        <code>bash &lt;(curl -fsSL railway.com/install.sh)</code> in a terminal
        and hit Recheck.
      </p>
      <button class="primary" onclick={startInstall}>Install the Railway CLI</button>
    {:else if install.state === "pending"}
      <p class="lede">Installing the Railway CLI… this takes a few seconds.</p>
      {#if install.output}<pre class="mono muted" style="white-space:pre-wrap">{install.output}</pre>{/if}
    {:else if install.state === "error"}
      <div class="banner err">{install.error || "Install failed."}</div>
      {#if install.output}<pre class="mono muted" style="white-space:pre-wrap">{install.output}</pre>{/if}
      <button class="primary" onclick={startInstall}>Try again</button>
    {:else if install.state === "done"}
      <p class="lede">Installed — refreshing status…</p>
    {/if}
  {:else if !railway?.authed}
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
    <label class="check"><input type="checkbox" bind:checked={seedInactive} /> Seed the inactive pool from the top TIG mainnet algorithm <span class="muted">(drawn on trajectory resets)</span></label>
    <label class="check"><input type="checkbox" bind:checked={seedPoolMainnet} /> Seed the initial pool from the top TIG mainnet algorithm <span class="muted">(fresh trajectories start from it)</span></label>
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
          <div class="trackrow">
            <span class="mono">solver timeout (s)</span>
            <input type="number" min="1" aria-label={`solver timeout for ${c}`} bind:value={timeoutEdits[c]} />
          </div>
        </div>
      {/each}
      <div class="hint">Benchmark instances per track for each challenge (0 disables a track), plus each solver's per-instance time budget in seconds.</div>
    </div>
  {/if}
  {#if adoptPrompt}
    <div class="adopt-warn">
      <h3>⚠ A swarm named “{swarmName}” already exists</h3>
      <p>Continuing will <strong>adopt</strong> it, not replace it:</p>
      <ul>
        <li>data volume, scores and seed pool are <strong>preserved</strong></li>
        <li>its admin key and swarm password are <strong>kept</strong> — contributors keep working</li>
        <li>the server is redeployed with the config above</li>
      </ul>
      {#if !adoptPrompt.is_yours}
        <p class="stern">
          This companion has no <code>swarm.admin.json</code> for that name, so it
          may belong to someone else in this Railway workspace. Adopting it will
          redeploy <em>their</em> live server.
        </p>
      {/if}
      <div class="actions">
        <div class="spacer"></div>
        <button onclick={() => (adoptPrompt = null)}>Cancel</button>
        <button class="primary" onclick={createSwarm}>Adopt and redeploy</button>
      </div>
    </div>
  {/if}
  <div class="actions">
    <div class="spacer"></div>
    <button class="primary"
            disabled={deploying || checkingName || !railway?.authed}
            onclick={createSwarm}>
      {deploying ? "Provisioning…" : checkingName ? "Checking name…" : "Provision on Railway"}
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
        <li><span>Admin key</span>
          <div class="credval">
            <code>{r.admin_key}</code>
            <button type="button" class="copybtn" onclick={() => copyText("admin_key", r.admin_key)}>{copied === "admin_key" ? "Copied" : "Copy"}</button>
          </div>
        </li>
        <li><span>Base password</span>
          <div class="credval">
            <code>{r.swarm_password}</code>
            <button type="button" class="copybtn" onclick={() => copyText("swarm_password", r.swarm_password)}>{copied === "swarm_password" ? "Copied" : "Copy"}</button>
          </div>
        </li>
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

    {#if seed?.configured}
      <div class="seedpool">
        <div class="row" style="align-items:baseline">
          <label style="flex:1">Seed pool</label>
          <label class="check" style="margin:0 12px 0 0"><input type="checkbox" bind:checked={reseedMainnet} /> from mainnet too</label>
          <button onclick={doReseed} disabled={reseeding}>
            {reseeding ? "Re-seeding…" : "Re-seed pool"}
          </button>
        </div>
        {#if emptyPool}
          <div class="banner warn" style="margin-top:10px">
            ⚠ Empty seed pool for {seed.empty.join(", ")} — agents fall back to the
            bare stub and can't produce a feasible solution. This happens after a
            server DB reset (only <code>create</code> repopulates seeds). Click
            <b>Re-seed pool</b> to restore the authored seeds.
          </div>
        {/if}
        <ul class="creds" style="margin-top:10px">
          {#each Object.keys(seed.authored ?? {}) as ch}
            <li>
              <span>{ch}</span>
              <b class={seed.pool_counts?.[ch] === 0 ? "bad" : ""}>
                {seed.pool_counts?.[ch] ?? "?"} in pool
                <span class="muted">({(seed.authored[ch] ?? []).join(", ")})</span>
              </b>
            </li>
          {/each}
        </ul>
        {#if seedMsg}<div class="banner ok" style="margin-top:10px">{seedMsg}</div>{/if}
      </div>
    {/if}

    <div class="actions">
      <div class="spacer"></div>
      <a class="btn" href={adminConsoleUrl()} target="_blank" rel="noreferrer">Open Admin Console →</a>
    </div>
  </div>
{/if}

<style>
  /* Adopt warning — reuses the shared warn tokens so it themes with everything
     else rather than hard-coding a colour. */
  .adopt-warn {
    background: var(--warn-bg);
    color: var(--warn);
    border-radius: 6px;
    padding: 12px 14px;
    margin-top: 16px;
    font-size: 13.5px;
  }
  .adopt-warn h3 { margin: 0 0 8px; font-size: 14px; }
  .adopt-warn p { margin: 6px 0; }
  .adopt-warn ul { margin: 6px 0 6px 18px; }
  .adopt-warn li { margin: 3px 0; }
  .adopt-warn .stern { border-top: 1px solid currentColor; margin-top: 10px; padding-top: 9px; }
  .adopt-warn .actions { margin-top: 12px; }
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
  .seedpool { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border-subtle); }
  .creds li b.bad { color: #c0392b; }
  .credval { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .credval code { overflow-wrap: anywhere; }
  .copybtn { flex: 0 0 auto; font-size: 12px; padding: 3px 9px; border: 1px solid var(--border-subtle); border-radius: 6px; background: transparent; color: var(--ink-dim); cursor: pointer; }
  .copybtn:hover { color: var(--ink); border-color: var(--ink-dim); }
  .tracks { margin: 4px 0 14px; }
  .trackgroup { padding: 8px 0; border-bottom: 1px solid var(--border-subtle); }
  .trackname { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  .trackrow { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 3px 0; font-size: 13px; }
  .trackrow input { width: 90px; flex: 0 0 auto; }
  .tracks .hint { font-size: 12.5px; color: var(--ink-dim); margin-top: 8px; }
</style>
