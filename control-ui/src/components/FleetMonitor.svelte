<script lang="ts">
  import { onMount } from "svelte";
  import LogStream from "./LogStream.svelte";
  import { localApi } from "../lib/api";
  import { ensureStream, fleetLog, fleetStatus } from "../lib/stream";

  // Reattachable fleet monitor. Safe to mount at any time (including a fresh
  // page load): it fetches the current fleet status from the companion and
  // (re)connects the event stream, which replays recent log history. So a
  // reload lands you right back on a live view with a working Stop button.
  let stopping = $state(false);
  let error = $state("");

  onMount(async () => {
    ensureStream(); // replays buffered log lines + status on connect
    try {
      fleetStatus.set(await localApi.fleetStatus());
    } catch (e: any) {
      error = e.message;
    }
  });

  async function stop() {
    stopping = true; error = "";
    try {
      fleetStatus.set(await localApi.fleetStop());
    } catch (e: any) {
      error = e.message;
    } finally {
      stopping = false;
    }
  }

  const agents = $derived(Object.entries($fleetStatus.agents || {}));
  const running = $derived($fleetStatus.running);
</script>

<div class="card">
  <div class="monitor-head">
    <h2>Fleet monitor</h2>
    <div class="spacer"></div>
    <span class="pill {$fleetStatus.state === 'running' ? 'ok' : $fleetStatus.state === 'error' ? 'err' : 'info'}">
      {$fleetStatus.state}
    </span>
    {#if running}
      <button class="danger" disabled={stopping} onclick={stop}>{stopping ? "Stopping…" : "■ Stop fleet"}</button>
    {/if}
  </div>

  {#if error}<div class="banner err">{error}</div>{/if}

  {#if agents.length}
    <div class="agentgrid">
      {#each agents as [name, a]}
        <div class="agentcard">
          <b>{name}</b>
          <span class="pill {(a as any).state === 'running' ? 'ok' : 'info'}">{(a as any).state}</span>
          {#if (a as any).pid}<span class="muted mono">pid {(a as any).pid}</span>{/if}
          {#if (a as any).returncode !== undefined && (a as any).returncode !== null}
            <span class="muted mono">rc {(a as any).returncode}</span>
          {/if}
        </div>
      {/each}
    </div>
  {:else}
    <p class="muted" style="margin-bottom:14px">No fleet is currently running.</p>
  {/if}

  <LogStream lines={$fleetLog} height="380px" />
</div>

<style>
  .monitor-head { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
  .monitor-head h2 { margin: 0; }
  .agentgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .agentcard { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: var(--bg-page); border: 1px solid var(--border-subtle); border-radius: 6px; font-size: 13px; }
  .agentcard b { flex: 1; }
</style>
