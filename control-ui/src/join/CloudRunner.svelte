<script lang="ts">
  // Tier-1 "run in the cloud" enrollment (P3). The swarm's hosted runner is a
  // separate service (runnerUrl); we call it cross-origin with the
  // contributor's credentials. Keys are POSTed straight to the runner, stored
  // encrypted there, and never touch this page's origin or localStorage.
  import { onMount } from "svelte";
  import { hostedApi } from "../lib/api";

  let { runnerUrl, username, password } = $props<{
    runnerUrl: string; username: string; password: string;
  }>();

  let loading = $state(true);
  let error = $state("");
  let status: any = $state(null);
  // The fleet plan (from the console) tells us which key env-vars to collect.
  let neededEnv: string[] = $state([]);
  let keyDraft: Record<string, string> = $state({});
  let busy = $state(false);

  async function refresh() {
    error = "";
    try {
      status = await hostedApi.runnerStatus(runnerUrl, username, password);
    } catch (e: any) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  async function loadNeededKeys() {
    // Derive the env-var names from the saved fleet plan; always include C3.
    try {
      const stored = await hostedApi.contributorConfigGet(username, password);
      const envs = new Set<string>(["C3_API_KEY"]);
      for (const a of stored?.config?.agents ?? []) {
        if (a.api_key_env) envs.add(a.api_key_env);
      }
      neededEnv = [...envs];
    } catch {
      neededEnv = ["C3_API_KEY"];
    }
  }

  onMount(async () => {
    await Promise.all([refresh(), loadNeededKeys()]);
  });

  async function enroll() {
    error = ""; busy = true;
    try {
      const keys: Record<string, string> = {};
      for (const name of neededEnv) {
        const v = (keyDraft[name] ?? "").trim();
        if (v) keys[name] = v;
      }
      await hostedApi.runnerEnroll(runnerUrl, username, password, keys);
      keyDraft = {};
      await refresh();
    } catch (e: any) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function unenroll() {
    if (!confirm("Stop your cloud fleet and delete your stored keys?")) return;
    busy = true;
    try {
      await hostedApi.runnerUnenroll(runnerUrl, username, password);
      await refresh();
    } catch (e: any) {
      error = e.message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="card">
  <h2>Run in the cloud</h2>
  <p class="lede">
    Let this swarm's host run your fleet — no install, nothing on your machine.
    Paste your API keys; they're stored <b>encrypted</b> on the runner and used
    only to run your agents. Use spend-limited keys where you can
    (<a href="https://openrouter.ai" target="_blank" rel="noopener">OpenRouter</a>
    supports per-key caps).
  </p>

  {#if error}<div class="banner err">{error}</div>{/if}

  {#if loading}
    <p class="muted">Loading…</p>
  {:else if status?.enrolled}
    <div class="banner ok">
      Your cloud fleet is <b>{status.status}</b> — {status.agents} agent(s).
      {#if status.last_error}<br />Last error: {status.last_error}{/if}
    </div>
    {#if status.keys && Object.keys(status.keys).length}
      <p class="lede">Stored keys: {#each Object.entries(status.keys) as [n, m]}<span class="mono">{n}={m}</span>&nbsp;{/each}</p>
    {/if}
    <div class="actions">
      <button disabled={busy} onclick={refresh}>↻ Refresh</button>
      <div class="spacer"></div>
      <button class="danger" disabled={busy} onclick={unenroll}>Stop &amp; remove keys</button>
      <button class="primary" disabled={busy} onclick={enroll}>Update keys &amp; restart</button>
    </div>
  {:else}
    <p class="lede">Configure agents under <b>My fleet</b> first, then paste the keys they need:</p>
    {#each neededEnv as name}
      <div class="field">
        <label for={"k-" + name}>{name}</label>
        <input id={"k-" + name} type="password" bind:value={keyDraft[name]}
          placeholder={name === "C3_API_KEY" ? "from cthree.cloud/dashboard/settings" : `paste ${name}`} />
      </div>
    {/each}
    <div class="actions">
      <div class="spacer"></div>
      <button class="primary" disabled={busy} onclick={enroll}>Start my cloud fleet</button>
    </div>
  {/if}
</div>
