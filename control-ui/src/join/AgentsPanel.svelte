<script lang="ts">
  // "My agents" status strip: the caller's registered agents, live from
  // /api/contributor/agents (contributor console, P1).
  import { onMount } from "svelte";
  import { hostedApi } from "../lib/api";

  let { username, password } = $props<{ username: string; password: string }>();

  let agents: any[] = $state([]);
  let loaded = $state(false);
  let error = $state("");

  async function refresh() {
    error = "";
    try {
      agents = (await hostedApi.contributorAgents(username, password)).agents ?? [];
    } catch (e: any) {
      error = e.message;
    } finally {
      loaded = true;
    }
  }

  onMount(refresh);

  const fmtTime = (t: string | null) => (t ? t.replace("T", " ").replace(/\..*/, "") : "—");
</script>

<div class="card">
  <div class="rowhead">
    <h2>My agents</h2>
    <div class="spacer"></div>
    <button onclick={refresh}>↻ Refresh</button>
  </div>
  {#if error}<div class="banner err">{error}</div>{/if}
  <table>
    <thead><tr><th>Name</th><th>Model</th><th>Status</th><th>Last heartbeat</th><th>State</th></tr></thead>
    <tbody>
      {#each agents as a}
        <tr>
          <td class="mono">{a.name}</td>
          <td class="muted">{a.llm_type ?? "—"}</td>
          <td>{a.status ?? "—"}</td>
          <td class="muted">{fmtTime(a.last_heartbeat)}</td>
          <td>
            {#if a.active}<span class="pill ok">active</span>
            {:else}<span class="pill info">idle</span>{/if}
          </td>
        </tr>
      {/each}
      {#if loaded && agents.length === 0}
        <tr><td colspan="5" class="muted">
          No agents yet — they appear here once your fleet registers
          (see Get started).
        </td></tr>
      {/if}
    </tbody>
  </table>
  <p class="lede" style="margin-top:10px">
    Scores and trajectories live on the
    <a href="/" target="_blank" rel="noopener">dashboard</a>.
  </p>
</div>
