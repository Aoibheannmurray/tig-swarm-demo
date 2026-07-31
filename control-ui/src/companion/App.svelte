<script lang="ts">
  import Masthead from "../components/Masthead.svelte";
  import Contributor from "./Contributor.svelte";
  import Host from "./Host.svelte";
  import ConfigEditor from "./ConfigEditor.svelte";
  import FleetMonitor from "../components/FleetMonitor.svelte";
  import { onMount } from "svelte";
  import { localApi } from "../lib/api";
  import { ensureStream, fleetStatus } from "../lib/stream";

  type View = "landing" | "contributor" | "host" | "fleet" | "editor";
  let view: View = $state("landing");
  // Credentials passed from Host → Contributor when a host self-invites, so the
  // wizard opens already connected instead of asking them to paste their own
  // password back in.
  let contributorPrefill: any = $state(null);
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
      view = "fleet";
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
    {:else}
      <!-- Admin mode: the console at /admin/ is served by this companion and
           proxies /api/admin/* to the swarm; it gates on the admin key at
           sign-in, so no extra auth is added here. -->
      <a class="btn" href="/admin/">Admin →</a>
    {/if}
  </Masthead>

  {#if error}<div class="banner err">{error}</div>{/if}

  {#if view === "landing"}
    <h3 class="secheading">Which are you?</h3>
    <div class="choices">
      <button class="choice" onclick={() => (view = "contributor")}>
        <span class="ch-head">
          <span class="pill">Contributor</span>
          <span class="spacer"></span>
          <span class="ch-arrow">→</span>
        </span>
        <span class="ch-title">Join a swarm</span>
        <span class="ch-desc">Someone sent you a join link.</span>
      </button>
      <button class="choice" onclick={() => (view = "host")}>
        <span class="ch-head">
          <span class="pill info">Host</span>
          <span class="spacer"></span>
          <span class="ch-arrow">→</span>
        </span>
        <span class="ch-title">Create a swarm</span>
        <span class="ch-desc">You're setting one up for others to join.</span>
      </button>
    </div>

    <!-- Entry point only. Everything you DO to a fleet — start, stop, monitor,
         reconfigure — lives on the fleet page, so the landing stays a choice
         rather than a choice plus a control panel. -->
    {#if fleetConfig}
      <h3 class="secheading">Your fleet</h3>
      <button class="card fleetlink" onclick={() => (view = "fleet")}>
        <span class="pill {running ? 'ok' : $fleetStatus.state === 'stopped' ? 'info' : 'warn'}">
          {running ? "running" : $fleetStatus.state === "stopped" ? "stopped" : "not running"}
        </span>
        <span class="fleetlink-txt">
          <b>{agentEntries.length}</b> {agentEntries.length === 1 ? "agent" : "agents"}
          <span class="muted">as</span> <b>{fleetConfig.username}</b>
        </span>
        <span class="spacer"></span>
        <!-- Named, not just an arrow: the row has to say where it goes, or
             "manage my fleet" has no obvious home on this page. -->
        <span class="fleetlink-go">Start, monitor &amp; edit fleet →</span>
      </button>
    {/if}

    {#if env}
      <div class="statusline muted">
        <span class="pill ok">companion online</span>
        {#if env.has_swarm_admin}<span>· host credentials found</span>{/if}
      </div>
    {:else}
      <div class="banner err">Can't reach the local companion. Start it with <code>python control_server.py</code>.</div>
    {/if}
  {:else if view === "fleet"}
    {#if !fleetConfig}
      <!-- Reachable if the config went away underneath us (removed on disk, a
           failed reconfigure). Without this the view matches but renders
           nothing, leaving a blank page with no way back but the masthead. -->
      <div class="card">
        <h2>No fleet configured</h2>
        <p class="lede">
          There's no <code>fleet.config.json</code> for this checkout yet. Join
          a swarm to create one.
        </p>
        <div class="actions">
          <button class="primary" onclick={() => (view = "contributor")}>Join a swarm →</button>
        </div>
      </div>
    {:else}
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
        {#if $fleetStatus.state === "stopping"}
          <!-- Agents don't die instantly. Without this the gap between the
               press and the state flip looked like the click was ignored. -->
          <button class="danger" disabled>Stopping…</button>
        {:else if running}
          <button class="danger" disabled={stopping} onclick={stopFleet}>{stopping ? "Stopping…" : "■ Stop fleet"}</button>
        {:else}
          <button class="primary" disabled={starting} onclick={startFleet}>{starting ? "Starting…" : "▶ Start fleet"}</button>
        {/if}
        <div class="spacer"></div>
        <button class="reconfig" onclick={() => (view = "editor")}>⚙ Reconfigure fleet</button>
      </div>
    </div>

    <!-- Same page: starting a fleet and watching it are one activity, so the
         log sits under the controls instead of behind a navigation step. -->
    <FleetMonitor embedded />
    {/if}
  {:else if view === "contributor"}
    <Contributor
      prefill={contributorPrefill}
      onLaunched={async () => { await refresh(); view = "fleet"; }} />
  {:else if view === "host"}
    <Host onJoinAsContributor={(creds) => { contributorPrefill = creds; view = "contributor"; }} />
  {:else if view === "editor"}
    <ConfigEditor
      onBack={() => (view = "fleet")}
      onStarted={async () => { await refresh(); view = "fleet"; }} />
  {/if}
</div>

<style>
  /* Section label — gives the page a visible spine (choose a role → your
     fleet) instead of a flat stack of cards. */
  .secheading {
    font-family: var(--ui); font-size: 11px; font-weight: 600;
    letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-dim);
    margin: 26px 0 12px;
  }

  .choices { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 720px) { .choices { grid-template-columns: 1fr; } }
  .choice {
    text-align: left; display: flex; flex-direction: column; gap: 10px;
    /* Deliberately large: this is the only decision on the page, and the tile
       is the target. min-height keeps the pair even when one line of copy
       wraps and the other doesn't. */
    padding: 30px 28px 32px; min-height: 210px;
    background: var(--bg-card); border: 1px solid var(--border-subtle);
    border-radius: var(--radius); box-shadow: var(--shadow); cursor: pointer;
    transition: border-color 0.15s, transform 0.08s, box-shadow 0.15s;
  }
  .choice:hover {
    border-color: var(--color-accent); transform: translateY(-2px);
    box-shadow: 0 2px 4px rgba(26, 26, 26, 0.06), 0 10px 28px rgba(26, 26, 26, 0.07);
  }
  .ch-head { display: flex; align-items: center; gap: 8px; }
  .ch-head .spacer { flex: 1; }
  .ch-arrow { color: var(--ink-faint); font-size: 19px; transition: color 0.15s, transform 0.15s; }
  .choice:hover .ch-arrow { color: var(--color-accent); transform: translateX(3px); }
  /* The title carries the tile — sized to be readable at a glance from across
     the viewport, with the one-line description as support. */
  .ch-title {
    font-family: var(--display); font-style: italic; font-size: 34px;
    font-weight: 600; line-height: 1.1; margin-top: auto;
  }
  .ch-desc { font-size: 15px; color: var(--ink-mid); }
  .statusline { display: flex; gap: 10px; align-items: center; margin-top: 22px; font-size: 13px; flex-wrap: wrap; }

  /* Landing entry point to the fleet page — status at a glance, no controls. */
  .fleetlink {
    display: flex; align-items: center; gap: 12px; width: 100%; text-align: left;
    padding: 16px 20px; font-family: var(--ui); font-size: 14px; font-weight: 400;
    cursor: pointer; transition: border-color 0.15s, transform 0.08s;
  }
  .fleetlink:hover { border-color: var(--color-accent); transform: translateY(-1px); background: var(--bg-card); }
  .fleetlink .spacer { flex: 1; }
  .fleetlink-txt b { font-weight: 600; }
  .fleetlink-go { color: var(--color-accent); font-weight: 600; white-space: nowrap; }
  @media (max-width: 560px) {
    .fleetlink { flex-wrap: wrap; }
    .fleetlink .spacer { display: none; }
  }

  /* Reconfigure was a .ghost (transparent, dimmed) — the least visible thing
     on the page, despite being the only route to changing the fleet. */
  .reconfig { border-color: var(--border-strong); color: var(--ink); }
  .reconfig:hover { border-color: var(--color-accent); color: var(--color-accent); }

  .fleetcard { border-color: var(--border-default); }
  .fleet-head h2 { margin: 0 0 2px; }
  .fleet-head { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 16px; }
  .swarm-meta { font-size: 13px; }
  .agentgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; margin-bottom: 4px; }
  .agentcard { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 11px 12px; background: var(--bg-page); border: 1px solid var(--border-subtle); border-radius: 6px; font-size: 13px; }
  .agentcard b { flex: 1; }
  .agent-sub { flex-basis: 100%; font-size: 11.5px; }
</style>
