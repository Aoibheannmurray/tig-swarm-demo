<script lang="ts">
  import { onMount } from "svelte";
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
  let activeChallenge = $state("");
  let stagThreshold = $state(2);
  let stagLimit = $state(10);
  let recallThreshold = $state(3);
  let seedInactive = $state(false);
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
      railway = await localApi.railwayStatus();
      challenges = await localApi.challenges();
      admin = await localApi.swarmAdmin();
      if (admin?.active_challenge) switchTo = admin.active_challenge;
    } catch (e: any) {
      error = e.message;
    }
  });

  async function createSwarm() {
    error = ""; deploying = true;
    try {
      ensureStream();
      await localApi.swarmCreate({
        swarm_type: swarmType,
        swarm_name: swarmName,
        active_challenge: activeChallenge,
        stagnation_threshold: stagThreshold,
        stagnation_limit: stagLimit,
        hypothesis_recall_threshold: recallThreshold,
        seed_inactive_pool: seedInactive,
      });
    } catch (e: any) {
      error = e.message;
      deploying = false;
    }
  }

  $effect(() => {
    if ($deployStatus.state === "done" || $deployStatus.state === "error") deploying = false;
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
    {/if}
  </div>
  {#if !railway?.authed}
    <p class="lede">
      Provisioning needs the Railway CLI, logged in. In your terminal run
      <code>railway login</code>, then reload this page.
      {#if railway?.message}<br /><span class="muted mono">{railway.message}</span>{/if}
    </p>
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
  .rowhead h2 { margin: 0; }
  .check { display: flex; align-items: center; gap: 8px; text-transform: none; letter-spacing: 0; font-weight: 500; color: var(--ink); }
  .check input { width: auto; }
  .creds { list-style: none; margin: 6px 0 4px; }
  .creds li { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0; border-bottom: 1px solid var(--border-subtle); font-size: 14px; }
  .creds li span { color: var(--ink-dim); }
</style>
