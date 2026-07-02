<script lang="ts">
  import Masthead from "../components/Masthead.svelte";
  import Contributor from "./Contributor.svelte";
  import Host from "./Host.svelte";
  import ConfigEditor from "./ConfigEditor.svelte";
  import FleetMonitor from "../components/FleetMonitor.svelte";
  import { onMount } from "svelte";
  import { localApi } from "../lib/api";
  import { ensureStream, fleetStatus } from "../lib/stream";

  type View = "landing" | "contributor" | "host" | "monitor" | "editor";
  let view: View = $state("landing");
  let env: any = $state(null);
  let fleetConfig: any = $state(null);
  let starting = $state(false);
  let stopping = $state(false);
  let error = $state("");

  const agentEntries = $derived(fleetConfig?.agents ?? []);
  const running = $derived($fleetStatus.running);

  async function refresh() {
    try {
      env = await localApi.env();
      const fc = await localApi.getFleetConfig();
      fleetConfig = fc.exists ? fc.config : null;
      fleetStatus.set(await localApi.fleetStatus());
    } catch {
      env = null;
    }
  }

  onMount(async () => {
    ensureStream(); // live status/logs flow into the stores even on the landing
    await refresh();
  });

  // Merge the configured agent with its live runtime state (if any).
  function liveState(name: string): string | null {
    const a = ($fleetStatus.agents || {})[name];
    return a ? a.state : null;
  }

  async function startFleet() {
    starting = true; error = "";
    try {
      ensureStream();
      await localApi.fleetStart();
      view = "monitor";
    } catch (e: any) {
      error = e.message;
    } finally {
      starting = false;
    }
  }

  async function stopFleet() {
    stopping = true; error = "";
    try {
      fleetStatus.set(await localApi.fleetStop());
    } catch (e: any) {
      error = e.message;
    } finally {
      stopping = false;
    }
  }
</script>

<div class="shell">
  <Masthead title="Prometheus" subtitle="Swarm Control">
    {#if view !== "landing"}
      <button class="ghost" onclick={() => { view = "landing"; refresh(); }}>← Home</button>
    {/if}
  </Masthead>

  {#if error}<div class="banner err">{error}</div>{/if}

  {#if view === "landing"}
    <!-- Your fleet / joined swarm -->
    {#if fleetConfig}
      <div class="card fleetcard">
        <div class="fleet-head">
          <div>
            <h2>Your fleet</h2>
            <div class="swarm-meta">
              <span class="muted">joined as</span> <b>{fleetConfig.username}</b>
              <span class="muted">·</span>
              <a href={fleetConfig.server_url} target="_blank" rel="noreferrer">{fleetConfig.server_url}</a>
            </div>
          </div>
          <div class="spacer"></div>
          <span class="pill {running ? 'ok' : $fleetStatus.state === 'stopped' ? 'info' : 'warn'}">
            {running ? "attached · running" : $fleetStatus.state === "stopped" ? "stopped" : "not running"}
          </span>
        </div>

        <div class="agentgrid">
          {#each agentEntries as a}
            {@const st = liveState(a.name)}
            <div class="agentcard">
              <b>{a.name}</b>
              <span class="pill {st === 'running' ? 'ok' : st === 'exited' ? 'info' : 'warn'}">{st ?? "idle"}</span>
              <div class="agent-sub muted mono">
                {a.provider}{a.model ? ` · ${a.model}` : ""} · {a.compute === "c3" ? `c3/${a.hardware ?? "auto"}` : "local"}
              </div>
            </div>
          {/each}
        </div>

        <div class="actions">
          {#if running}
            <button class="primary" onclick={() => (view = "monitor")}>Open monitor →</button>
            <button class="danger" disabled={stopping} onclick={stopFleet}>{stopping ? "Stopping…" : "■ Stop fleet"}</button>
          {:else}
            <button class="primary" disabled={starting} onclick={startFleet}>{starting ? "Starting…" : "▶ Start fleet"}</button>
            <button onclick={() => (view = "monitor")}>View last run</button>
          {/if}
          <div class="spacer"></div>
          <button class="ghost" onclick={() => (view = "editor")}>Reconfigure</button>
        </div>
      </div>
    {/if}

    <div class="card intro">
      <h2>{fleetConfig ? "Do more" : "Run the swarm without the command line"}</h2>
      <p class="lede">
        A local companion for standing up and joining a Prometheus swarm. The
        classic CLI (<code>python setup.py</code>, <code>python run.py</code>)
        still works if you prefer it.
      </p>
    </div>

    <div class="choices">
      <button class="choice" onclick={() => (view = "contributor")}>
        <span class="pill">Contributor</span>
        <span class="ch-title">{fleetConfig ? "Join another / reconfigure" : "Join a swarm"}</span>
        <span class="ch-desc">Configure your agents (provider, model, count, compute), add tacit knowledge, then launch.</span>
      </button>
      <button class="choice" onclick={() => (view = "host")}>
        <span class="pill info">Host</span>
        <span class="ch-title">Create &amp; manage a swarm</span>
        <span class="ch-desc">Provision on Railway, pick the active challenge, seed the pool, switch challenges, open the Admin Console.</span>
      </button>
    </div>

    {#if env}
      <div class="statusline muted">
        <span class="pill ok">companion online</span>
        {#if env.has_swarm_admin}<span>· host credentials found</span>{/if}
      </div>
    {:else}
      <div class="banner err">Can't reach the local companion. Start it with <code>python control_server.py</code>.</div>
    {/if}
  {:else if view === "contributor"}
    <Contributor />
  {:else if view === "host"}
    <Host />
  {:else if view === "editor"}
    <ConfigEditor />
  {:else if view === "monitor"}
    <FleetMonitor />
  {/if}
</div>

<style>
  .intro { background: transparent; border: none; box-shadow: none; padding: 8px 0 4px; }
  .choices { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 720px) { .choices { grid-template-columns: 1fr; } }
  .choice {
    text-align: left; display: flex; flex-direction: column; gap: 8px; padding: 22px;
    background: var(--bg-card); border: 1px solid var(--border-subtle);
    border-radius: var(--radius); box-shadow: var(--shadow); cursor: pointer;
    transition: border-color 0.15s, transform 0.08s;
  }
  .choice:hover { border-color: var(--color-accent); transform: translateY(-1px); }
  .ch-title { font-family: var(--display); font-style: italic; font-size: 22px; font-weight: 600; }
  .ch-desc { font-size: 13.5px; color: var(--ink-mid); }
  .statusline { display: flex; gap: 10px; align-items: center; margin-top: 22px; font-size: 13px; flex-wrap: wrap; }

  .fleetcard { border-color: var(--border-default); }
  .fleet-head { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 16px; }
  .fleet-head h2 { margin: 0 0 2px; }
  .swarm-meta { font-size: 13px; }
  .agentgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; margin-bottom: 4px; }
  .agentcard { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 11px 12px; background: var(--bg-page); border: 1px solid var(--border-subtle); border-radius: 6px; font-size: 13px; }
  .agentcard b { flex: 1; }
  .agent-sub { flex-basis: 100%; font-size: 11.5px; }
</style>
