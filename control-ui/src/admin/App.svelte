<script lang="ts">
  import { onMount } from "svelte";
  import Masthead from "../components/Masthead.svelte";
  import { hostedApi, hostedBase, deriveInvitePassword, buildJoinLink, localApi } from "../lib/api";

  let adminKey = $state(sessionStorage.getItem("prom_admin_key") ?? "");
  let basePassword = $state(sessionStorage.getItem("prom_base_pw") ?? "");
  let authed = $state(false);
  let error = $state("");
  let busy = $state(false);

  let contributors: any[] = $state([]);
  let config: any = $state(null);
  let tab: "contributors" | "challenge" | "broadcast" | "pools" | "settings" = $state("contributors");

  // The swarm's PUBLIC url — what invites must carry so contributors can reach
  // it. When this console is served by the local companion (at /admin/),
  // location.origin is the companion (e.g. http://127.0.0.1:8790), NOT the
  // swarm — so ask the companion for the real url. When served directly by the
  // hosted swarm server, /local-api doesn't exist, the fetch fails, and we fall
  // back to location.origin, which there IS the swarm url. An explicit
  // ?server= override always wins.
  let swarmUrl = $state(hostedBase());
  onMount(async () => {
    if (swarmUrl) return; // ?server= override supplied
    try {
      const env = await localApi.env();
      if (env?.server_url) swarmUrl = String(env.server_url).replace(/\/$/, "");
    } catch {
      /* served by the hosted swarm itself — location.origin is correct */
    }
  });

  const serverLabel = () => swarmUrl || location.origin;

  async function authenticate() {
    error = ""; busy = true;
    try {
      const res = await hostedApi.contributors(adminKey);
      contributors = res.contributors ?? [];
      config = await hostedApi.swarmConfig();
      authed = true;
      sessionStorage.setItem("prom_admin_key", adminKey);
      sessionStorage.setItem("prom_base_pw", basePassword);
    } catch (e: any) {
      error = `Auth failed: ${e.message}`;
      authed = false;
    } finally {
      busy = false;
    }
  }

  function logout() {
    sessionStorage.removeItem("prom_admin_key");
    sessionStorage.removeItem("prom_base_pw");
    authed = false; adminKey = ""; contributors = [];
  }

  async function refreshContributors() {
    try { contributors = (await hostedApi.contributors(adminKey)).contributors ?? []; }
    catch (e: any) { error = e.message; }
  }

  // ── Invite ──
  let inviteName = $state("");
  let inviteBlock = $state("");
  let inviteLink = $state("");
  async function makeInvite() {
    error = "";
    if (!basePassword) { error = "Enter the base swarm password (from create) to generate invites."; return; }
    if (!inviteName.trim()) { error = "Enter a username for the invite."; return; }
    try {
      const pw = await deriveInvitePassword(inviteName.trim(), basePassword);
      inviteBlock = `"server_url": "${serverLabel()}",\n"username": "${inviteName.trim()}",\n"swarm_password": "${pw}"`;
      inviteLink = buildJoinLink(serverLabel(), inviteName.trim(), pw);
    } catch (e: any) { error = e.message; }
  }

  // ── Bulk / random invites ──
  // Same derivation as the single invite (sha256(username:base), local-only).
  // Random usernames use adjective-noun slugs like the CLI's `setup.py invite`;
  // a prefix switches to numbered names (prefix-1 … prefix-N).
  const INVITE_ADJECTIVES = ["swift", "quiet", "brave", "merry", "keen", "vivid",
    "lucky", "bold", "sunny", "witty", "calm", "nimble", "spry", "zesty"];
  const INVITE_NOUNS = ["otter", "falcon", "badger", "lynx", "heron", "marmot",
    "puffin", "gecko", "wombat", "ibex", "stoat", "kestrel", "tapir", "quokka"];
  let bulkCount = $state(1);
  let bulkPrefix = $state("");
  let bulkInvites: { username: string; link: string }[] = $state([]);
  function randomSlug(taken: Set<string>): string {
    for (let i = 0; i < 40; i++) {
      const name = `${INVITE_ADJECTIVES[Math.floor(Math.random() * INVITE_ADJECTIVES.length)]}-${INVITE_NOUNS[Math.floor(Math.random() * INVITE_NOUNS.length)]}`;
      if (!taken.has(name)) return name;
    }
    return `contrib-${10000 + Math.floor(Math.random() * 90000)}`;
  }
  async function makeBulkInvites() {
    error = "";
    if (!basePassword) { error = "Enter the base swarm password (from create) to generate invites."; return; }
    const n = Math.max(1, Math.min(50, Math.floor(bulkCount) || 1));
    const taken = new Set<string>(contributors.map((c) => c.username));
    const names: string[] = [];
    for (let i = 1; i <= n; i++) {
      const name = bulkPrefix.trim() ? `${bulkPrefix.trim()}-${i}` : randomSlug(taken);
      taken.add(name);
      names.push(name);
    }
    try {
      bulkInvites = await Promise.all(names.map(async (username) => ({
        username,
        link: buildJoinLink(serverLabel(), username, await deriveInvitePassword(username, basePassword)),
      })));
    } catch (e: any) { error = e.message; }
  }
  const bulkText = $derived(bulkInvites.map((b) => `${b.username}: ${b.link}`).join("\n"));

  async function revoke(username: string) {
    if (!confirm(`Revoke ${username}? This kills their running agents.`)) return;
    try { await hostedApi.revoke(adminKey, username); await refreshContributors(); }
    catch (e: any) { error = e.message; }
  }

  // ── Challenge ──
  let newActive = $state("");
  let challengeMsg = $state("");
  $effect(() => { if (config && !newActive) newActive = config.active_challenge; });
  async function switchChallenge() {
    challengeMsg = ""; error = "";
    try {
      await hostedApi.setActiveChallenge(adminKey, newActive);
      config = await hostedApi.swarmConfig();
      challengeMsg = `Active challenge → ${config.active_challenge}`;
    } catch (e: any) { error = e.message; }
  }

  // ── Broadcast ──
  let msg = $state("");
  let priority = $state("normal");
  let broadcastMsg = $state("");
  async function sendBroadcast() {
    broadcastMsg = ""; error = "";
    try { await hostedApi.broadcast(adminKey, msg, priority); broadcastMsg = "Broadcast sent."; msg = ""; }
    catch (e: any) { error = e.message; }
  }

  // ── Pools ──
  let poolChallenge = $state("");
  let poolMsg = $state("");
  $effect(() => { if (config && !poolChallenge) poolChallenge = config.active_challenge; });

  // Seed pool listing (read-only). Loaded when the Pools tab is open and the
  // challenge picker changes; older servers without /api/admin/seeds get a
  // graceful "not supported" note instead of an error banner.
  let seeds: any[] = $state([]);
  let seedsErr = $state("");
  let seedsLoading = $state(false);
  async function loadSeeds() {
    seedsErr = ""; seedsLoading = true;
    try {
      seeds = (await hostedApi.listSeeds(adminKey, poolChallenge)).seeds ?? [];
    } catch (e: any) {
      seeds = [];
      seedsErr = /404|Not Found/i.test(e.message)
        ? "This swarm server predates the seed-pool listing — redeploy it to enable this view."
        : e.message;
    } finally {
      seedsLoading = false;
    }
  }
  $effect(() => { if (authed && tab === "pools" && poolChallenge) loadSeeds(); });
  async function pool(action: "seed" | "clear" | "reset") {
    poolMsg = ""; error = "";
    try {
      if (action === "seed") await hostedApi.seedInactive(adminKey, poolChallenge);
      else if (action === "clear") await hostedApi.clearInactive(adminKey, poolChallenge);
      else {
        if (!confirm(`Reset the ${poolChallenge} leaderboard? This clears its best history.`)) return;
        await hostedApi.resetChallenge(adminKey, poolChallenge);
      }
      poolMsg = `${action} on ${poolChallenge} done.`;
    } catch (e: any) { error = e.message; }
  }

  let challengeNames = $derived(config ? Object.keys(config.available_challenges ?? {}) : []);

  // ── Settings (stagnation + HPO knobs, hot-editable) ──
  // Every knob is read by contributors from the pushed swarm config on their
  // next iteration/state poll, so edits apply without restarting the swarm.
  const KNOBS: { key: string; label: string; hint: string }[] = [
    { key: "stagnation_threshold", label: "Stagnation threshold",
      hint: "iterations without improvement before an agent is treated as stagnating (hints kick in)" },
    { key: "stagnation_limit", label: "Stagnation limit",
      hint: "iterations without improvement before the trajectory is reset (0 = never reset)" },
    { key: "negative_trajectory_limit", label: "Negative-trajectory limit",
      hint: "edits allowed while a trajectory's best is still ≤ 0 before reset; 0 = off — keep OFF on challenges with inherently negative scores (e.g. neuralnet)" },
    { key: "hypothesis_recall_threshold", label: "Hypothesis recall threshold",
      hint: "how many past hypotheses are recalled into agent prompts" },
    { key: "hpo_first_tune_improvements", label: "HPO: first-tune improvements",
      hint: "improvements a trajectory needs before its FIRST hyperparameter tune" },
    { key: "hpo_min_improvements", label: "HPO: min improvements",
      hint: "improvements needed for later tunes; also sets the tune-band width" },
    { key: "hpo_search_budget", label: "HPO: search budget",
      hint: "hyperparameter configs evaluated per tune" },
    { key: "hpo_num_suggested_configs", label: "HPO: LLM-suggested configs",
      hint: "max LLM-suggested configs folded into the search budget" },
  ];
  let knobDraft: Record<string, number> = $state({});
  let settingsMsg = $state("");
  $effect(() => {
    if (config && tab === "settings") {
      for (const k of KNOBS) {
        if (knobDraft[k.key] === undefined) knobDraft[k.key] = config[k.key];
      }
    }
  });
  async function saveSettings() {
    settingsMsg = ""; error = "";
    const changed: Record<string, number> = {};
    for (const k of KNOBS) {
      const v = knobDraft[k.key];
      if (v !== undefined && v !== null && Number.isFinite(v) && v !== config[k.key]) {
        changed[k.key] = Math.floor(v);
      }
    }
    if (!Object.keys(changed).length) { settingsMsg = "Nothing changed."; return; }
    try {
      await hostedApi.updateSwarmConfig(adminKey, changed);
      config = await hostedApi.swarmConfig();
      settingsMsg = `Saved: ${Object.keys(changed).join(", ")}. Agents pick this up on their next iteration.`;
    } catch (e: any) { error = e.message; }
  }

  // ── Instances (tracks) editor ──
  // A challenge's `tracks` maps track label → instance count, plus non-numeric
  // bookkeeping entries (e.g. "seed": "test") which must be preserved as-is.
  let trackChallenge = $state("");
  let trackDraft: Record<string, number> = $state({});
  let trackMsg = $state("");
  $effect(() => { if (config && !trackChallenge) trackChallenge = config.active_challenge; });
  const trackSource = $derived(
    (config?.available_challenges?.[trackChallenge]?.tracks ?? {}) as Record<string, any>);
  const numericTracks = $derived(
    Object.keys(trackSource).filter((k) => typeof trackSource[k] === "number"));
  $effect(() => {
    // Re-key the draft when the selected challenge changes.
    trackChallenge;
    trackDraft = Object.fromEntries(numericTracks.map((k) => [k, trackSource[k]]));
  });
  async function saveTracks() {
    trackMsg = ""; error = "";
    const tracks: Record<string, any> = { ...trackSource };
    for (const k of numericTracks) {
      const v = Math.floor(trackDraft[k]);
      if (!Number.isFinite(v) || v < 0) { error = `Invalid count for ${k}.`; return; }
      tracks[k] = v;
    }
    try {
      await hostedApi.updateSwarmConfig(adminKey, { challenges: { [trackChallenge]: { tracks } } });
      config = await hostedApi.swarmConfig();
      trackMsg = `Instances saved for ${trackChallenge} — applies to the next benchmark each agent runs.`;
    } catch (e: any) { error = e.message; }
  }

  function fmtTime(t: string | null): string {
    if (!t) return "—";
    try { return new Date(t).toLocaleString(); } catch { return t; }
  }
</script>

<div class="shell">
  <Masthead title="Prometheus" subtitle="Admin Console">
    {#if authed}<button class="ghost" onclick={logout}>Sign out</button>{/if}
  </Masthead>

  {#if error}<div class="banner err">{error}</div>{/if}

  {#if !authed}
    <div class="card" style="max-width:520px;margin:0 auto">
      <h2>Sign in</h2>
      <p class="lede">Manage <b>{serverLabel()}</b>. Keys are kept in this browser tab only.</p>
      <div class="field">
        <label for="ak">Admin key</label>
        <input id="ak" type="password" bind:value={adminKey} placeholder="from setup.py create" />
      </div>
      <div class="field">
        <label for="bp">Base swarm password <span class="muted" style="text-transform:none">(optional — needed to generate invites)</span></label>
        <input id="bp" type="password" bind:value={basePassword} />
      </div>
      <div class="actions">
        <div class="spacer"></div>
        <button class="primary" disabled={busy || !adminKey} onclick={authenticate}>{busy ? "Checking…" : "Sign in"}</button>
      </div>
    </div>
  {:else}
    <nav class="tabs">
      <button class:active={tab === "contributors"} onclick={() => (tab = "contributors")}>Contributors</button>
      <button class:active={tab === "challenge"} onclick={() => (tab = "challenge")}>Challenge</button>
      <button class:active={tab === "broadcast"} onclick={() => (tab = "broadcast")}>Broadcast</button>
      <button class:active={tab === "pools"} onclick={() => (tab = "pools")}>Pools</button>
      <button class:active={tab === "settings"} onclick={() => (tab = "settings")}>Settings</button>
    </nav>

    {#if tab === "contributors"}
      <div class="card">
        <div class="rowhead"><h2>Contributors</h2><div class="spacer"></div><button onclick={refreshContributors}>↻ Refresh</button></div>
        <table>
          <thead><tr><th>Username</th><th>Agents</th><th>Active</th><th>Last heartbeat</th><th>State</th><th></th></tr></thead>
          <tbody>
            {#each contributors as c}
              <tr>
                <td class="mono">{c.username}</td>
                <td>{c.agent_count}</td>
                <td>{c.agents_active}</td>
                <td class="muted">{fmtTime(c.last_heartbeat)}</td>
                <td>
                  {#if c.revoked}<span class="pill err">revoked</span>
                  {:else if c.agents_active > 0}<span class="pill ok">active</span>
                  {:else}<span class="pill info">idle</span>{/if}
                </td>
                <td>{#if !c.revoked}<button class="danger" onclick={() => revoke(c.username)}>Revoke</button>{/if}</td>
              </tr>
            {/each}
            {#if contributors.length === 0}<tr><td colspan="6" class="muted">No contributors yet.</td></tr>{/if}
          </tbody>
        </table>
      </div>

      <div class="card">
        <h2>Generate an invite</h2>
        <p class="lede">Derives the contributor's per-swarm password locally (never sent to the server).</p>
        <div class="row" style="align-items:flex-end">
          <div class="field" style="margin-bottom:0"><label for="iv">Username</label><input id="iv" type="text" bind:value={inviteName} /></div>
          <div style="flex:0 0 auto"><button class="primary" onclick={makeInvite}>Create invite</button></div>
        </div>
        {#if inviteLink}
          <div class="field" style="margin-top:16px">
            <label for="il">Join link — share this one line (it contains their password)</label>
            <textarea id="il" readonly rows="2">{inviteLink}</textarea>
            <button class="ghost" onclick={() => navigator.clipboard.writeText(inviteLink)}>Copy link</button>
          </div>
        {/if}
        {#if inviteBlock}
          <div class="field" style="margin-top:16px">
            <label for="ib">Or the manual config lines</label>
            <textarea id="ib" readonly rows="3">{inviteBlock}</textarea>
            <button class="ghost" onclick={() => navigator.clipboard.writeText(inviteBlock)}>Copy</button>
          </div>
        {/if}
      </div>

      <div class="card">
        <h2>Batch invites</h2>
        <p class="lede">
          Generate several invites at once — random names, or numbered ones
          when you set a prefix (<span class="mono">team → team-1 … team-N</span>).
          Derived locally, same as single invites.
        </p>
        <div class="row" style="align-items:flex-end">
          <div class="field" style="margin-bottom:0;max-width:130px">
            <label for="bc">How many</label>
            <input id="bc" type="number" min="1" max="50" bind:value={bulkCount} />
          </div>
          <div class="field" style="margin-bottom:0">
            <label for="bpfx">Prefix <span class="muted" style="text-transform:none">(optional — blank = random names)</span></label>
            <input id="bpfx" type="text" bind:value={bulkPrefix} placeholder="e.g. team" />
          </div>
          <div style="flex:0 0 auto"><button class="primary" onclick={makeBulkInvites}>Generate</button></div>
        </div>
        {#if bulkInvites.length}
          <div class="field" style="margin-top:16px">
            <label for="blk">{bulkInvites.length} join link(s) — one per line (each contains that user's password)</label>
            <textarea id="blk" readonly rows={Math.min(10, bulkInvites.length + 1)}>{bulkText}</textarea>
            <button class="ghost" onclick={() => navigator.clipboard.writeText(bulkText)}>Copy all</button>
          </div>
        {/if}
      </div>
    {:else if tab === "challenge"}
      <div class="card">
        <h2>Active challenge</h2>
        <p class="lede">Currently <b>{config?.active_challenge}</b>. Contributors auto-follow on their next iteration.</p>
        <div class="row" style="align-items:flex-end">
          <div class="field" style="margin-bottom:0">
            <label for="nc">Switch to</label>
            <select id="nc" bind:value={newActive}>{#each challengeNames as c}<option value={c}>{c}</option>{/each}</select>
          </div>
          <div style="flex:0 0 auto"><button class="primary" onclick={switchChallenge}>Switch</button></div>
        </div>
        {#if challengeMsg}<div class="banner ok" style="margin-top:14px">{challengeMsg}</div>{/if}
      </div>

      <div class="card">
        <h2>Instances per track</h2>
        <p class="lede">
          How many instances each benchmark runs per track. Hot-editable —
          contributors pick the new counts up on their next sync (more
          instances = more robust scores, longer benchmarks).
        </p>
        <div class="field" style="max-width:260px">
          <label for="tc">Challenge</label>
          <select id="tc" bind:value={trackChallenge}>{#each challengeNames as c}<option value={c}>{c}</option>{/each}</select>
        </div>
        {#each numericTracks as t}
          <div class="row" style="align-items:center">
            <div class="mono" style="flex:1">{t}</div>
            <div class="field" style="margin-bottom:0;max-width:130px">
              <input type="number" min="0" aria-label={`instances for ${t}`} bind:value={trackDraft[t]} />
            </div>
          </div>
        {/each}
        {#if numericTracks.length === 0}
          <p class="muted">No instance-count tracks configured for this challenge.</p>
        {:else}
          <div class="actions"><div class="spacer"></div><button class="primary" onclick={saveTracks}>Save instances</button></div>
        {/if}
        {#if trackMsg}<div class="banner ok" style="margin-top:14px">{trackMsg}</div>{/if}
      </div>
    {:else if tab === "broadcast"}
      <div class="card">
        <h2>Broadcast to dashboards</h2>
        <p class="lede">Appears live on every connected dashboard.</p>
        <div class="field"><label for="bm">Message</label><textarea id="bm" bind:value={msg}></textarea></div>
        <div class="field" style="max-width:200px">
          <label for="pr">Priority</label>
          <select id="pr" bind:value={priority}><option value="normal">Normal</option><option value="high">High</option></select>
        </div>
        <div class="actions"><div class="spacer"></div><button class="primary" disabled={!msg.trim()} onclick={sendBroadcast}>Send broadcast</button></div>
        {#if broadcastMsg}<div class="banner ok">{broadcastMsg}</div>{/if}
      </div>
    {:else if tab === "pools"}
      <div class="card">
        <h2>Seed &amp; reset pools</h2>
        <div class="field" style="max-width:260px">
          <label for="pc">Challenge</label>
          <select id="pc" bind:value={poolChallenge}>{#each challengeNames as c}<option value={c}>{c}</option>{/each}</select>
        </div>
        <div class="actions" style="justify-content:flex-start">
          <button onclick={() => pool("seed")}>Seed inactive from mainnet</button>
          <button onclick={() => pool("clear")}>Clear inactive pool</button>
          <button class="danger" onclick={() => pool("reset")}>Reset leaderboard</button>
        </div>
        {#if poolMsg}<div class="banner ok" style="margin-top:14px">{poolMsg}</div>{/if}
      </div>

      <div class="card">
        <div class="rowhead">
          <h2>Seed pool <span class="muted">({seeds.length})</span></h2>
          <div class="spacer"></div>
          <button onclick={loadSeeds} disabled={seedsLoading}>{seedsLoading ? "Loading…" : "↻ Refresh"}</button>
        </div>
        <p class="lede">
          Working starter algorithms handed out on fresh <b>{poolChallenge}</b>
          trajectories (seed → best peer → stub). An empty pool means seeded
          agents fall back to a peer's code, or the bare stub if none exists.
        </p>
        {#if seedsErr}<div class="banner err">{seedsErr}</div>{/if}
        <table>
          <thead><tr><th>Tag</th><th>Source</th><th>Score</th><th>Feasible</th><th>Size</th><th>Origin</th><th>Created</th></tr></thead>
          <tbody>
            {#each seeds as s}
              <tr>
                <td class="mono">{s.strategy_tag}</td>
                <td><span class="pill {s.source === 'authored' ? 'info' : 'ok'}">{s.source}</span></td>
                <td>{s.score ?? "—"}</td>
                <td>{#if s.feasible}<span class="pill ok">yes</span>{:else}<span class="pill err">no</span>{/if}</td>
                <td class="muted">{Math.round(s.code_chars / 1000)}k{s.kernel_chars ? ` + ${Math.round(s.kernel_chars / 1000)}k cu` : ""}{s.multi_file ? " · multi-file" : ""}</td>
                <td class="mono muted">{s.origin_agent_id ?? "—"}</td>
                <td class="muted">{fmtTime(s.created_at)}</td>
              </tr>
            {/each}
            {#if seeds.length === 0 && !seedsErr}
              <tr><td colspan="7" class="muted">
                {seedsLoading ? "Loading…" : "Seed pool is empty — deposit one via setup.py or POST /api/admin/seed_pool."}
              </td></tr>
            {/if}
          </tbody>
        </table>
      </div>
    {:else if tab === "settings"}
      <div class="card">
        <h2>Swarm settings</h2>
        <p class="lede">
          Stagnation and hyperparameter-optimization knobs. All hot-editable:
          contributors read them from the swarm config on every iteration, so
          changes apply without restarting anything.
        </p>
        {#each KNOBS as k}
          <div class="row knobrow" style="align-items:center">
            <div style="flex:1">
              <div>{k.label} <span class="mono muted">({k.key})</span></div>
              <div class="hint" style="margin-top:2px">{k.hint}</div>
            </div>
            <div class="field" style="margin-bottom:0;max-width:130px">
              <input type="number" min="0" aria-label={k.label} bind:value={knobDraft[k.key]} />
            </div>
          </div>
        {/each}
        <div class="actions"><div class="spacer"></div><button class="primary" onclick={saveSettings}>Save settings</button></div>
        {#if settingsMsg}<div class="banner ok" style="margin-top:14px">{settingsMsg}</div>{/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  .tabs { display: flex; gap: 6px; margin-bottom: 20px; border-bottom: 1px solid var(--border-subtle); }
  .tabs button {
    background: transparent; border: none; border-bottom: 2px solid transparent;
    border-radius: 0; padding: 10px 14px; color: var(--ink-dim); font-weight: 600;
  }
  .tabs button.active { color: var(--ink); border-bottom-color: var(--color-accent); }
  .rowhead { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .knobrow { padding: 10px 0; border-bottom: 1px solid var(--border-subtle); }
  .rowhead h2 { margin: 0; }
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  th { text-align: left; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-dim); padding: 8px 10px; border-bottom: 1px solid var(--border-default); }
  td { padding: 9px 10px; border-bottom: 1px solid var(--border-subtle); }
</style>
