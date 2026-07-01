<script lang="ts">
  import Masthead from "../components/Masthead.svelte";
  import Contributor from "./Contributor.svelte";
  import Host from "./Host.svelte";
  import { onMount } from "svelte";
  import { localApi } from "../lib/api";

  type View = "landing" | "contributor" | "host";
  let view: View = $state("landing");
  let env: any = $state(null);

  onMount(async () => {
    try {
      env = await localApi.env();
    } catch {
      env = null;
    }
  });
</script>

<div class="shell">
  <Masthead title="Prometheus" subtitle="Swarm Control">
    {#if view !== "landing"}
      <button class="ghost" onclick={() => (view = "landing")}>← Home</button>
    {/if}
  </Masthead>

  {#if view === "landing"}
    <div class="card intro">
      <h2>Run the swarm without the command line</h2>
      <p class="lede">
        A local companion for standing up and joining a Prometheus swarm — the
        setup that has to happen on your own machine. The classic CLI
        (<code>python setup.py</code>, <code>python run.py</code>) still works if
        you prefer it.
      </p>
    </div>

    <div class="choices">
      <button class="choice" onclick={() => (view = "contributor")}>
        <span class="pill">Contributor</span>
        <span class="ch-title">Join a swarm</span>
        <span class="ch-desc">
          Configure your agents (provider, model, count, compute), add tacit
          knowledge, then launch and watch your fleet live.
        </span>
      </button>

      <button class="choice" onclick={() => (view = "host")}>
        <span class="pill info">Host</span>
        <span class="ch-title">Create &amp; manage a swarm</span>
        <span class="ch-desc">
          Provision a new swarm on Railway, pick the active challenge, seed the
          pool, switch challenges, and open the Admin Console.
        </span>
      </button>
    </div>

    {#if env}
      <div class="statusline muted">
        <span class="pill ok">companion online</span>
        {#if env.has_fleet_config}<span>· fleet.config.json found</span>{/if}
        {#if env.has_swarm_admin}<span>· host credentials found</span>{/if}
        {#if env.server_url}<span>· {env.server_url}</span>{/if}
      </div>
    {:else}
      <div class="banner err">
        Can't reach the local companion. Start it with
        <code>python control_server.py</code>.
      </div>
    {/if}
  {:else if view === "contributor"}
    <Contributor />
  {:else if view === "host"}
    <Host />
  {/if}
</div>

<style>
  .intro { background: transparent; border: none; box-shadow: none; padding: 8px 0 4px; }
  .choices { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 720px) { .choices { grid-template-columns: 1fr; } }
  .choice {
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 22px;
    background: var(--bg-card);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    cursor: pointer;
    transition: border-color 0.15s, transform 0.08s;
  }
  .choice:hover { border-color: var(--color-accent); background: var(--bg-card); transform: translateY(-1px); }
  .ch-title { font-family: var(--display); font-style: italic; font-size: 22px; font-weight: 600; }
  .ch-desc { font-size: 13.5px; color: var(--ink-mid); }
  .statusline { display: flex; gap: 10px; align-items: center; margin-top: 22px; font-size: 13px; flex-wrap: wrap; }
</style>
