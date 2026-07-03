<script lang="ts">
  import { onMount } from "svelte";
  import Masthead from "../components/Masthead.svelte";
  import { hostedApi, hostedBase, deriveInvitePassword, localApi } from "../lib/api";

  let adminKey = $state(sessionStorage.getItem("prom_admin_key") ?? "");
  let basePassword = $state(sessionStorage.getItem("prom_base_pw") ?? "");
  let authed = $state(false);
  let error = $state("");
  let busy = $state(false);

  let contributors: any[] = $state([]);
  let config: any = $state(null);
  let tab: "contributors" | "challenge" | "broadcast" | "pools" = $state("contributors");

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
  async function makeInvite() {
    error = "";
    if (!basePassword) { error = "Enter the base swarm password (from create) to generate invites."; return; }
    if (!inviteName.trim()) { error = "Enter a username for the invite."; return; }
    const pw = await deriveInvitePassword(inviteName.trim(), basePassword);
    inviteBlock = `"server_url": "${serverLabel()}",\n"username": "${inviteName.trim()}",\n"swarm_password": "${pw}"`;
  }

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
        {#if inviteBlock}
          <div class="field" style="margin-top:16px">
            <label for="ib">Share these lines</label>
            <textarea id="ib" readonly rows="3">{inviteBlock}</textarea>
            <button class="ghost" onclick={() => navigator.clipboard.writeText(inviteBlock)}>Copy</button>
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
  .rowhead h2 { margin: 0; }
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  th { text-align: left; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-dim); padding: 8px 10px; border-bottom: 1px solid var(--border-default); }
  td { padding: 9px 10px; border-bottom: 1px solid var(--border-subtle); }
</style>
