<script lang="ts">
  // Hosted fleet-plan editor (contributor console, P1). Edits the same
  // agents-array shape as fleet.config.json and PUTs it to
  // /api/contributor/config. Secrets never pass through here: LLM keys are
  // referenced by env-var NAME, and the server rejects anything else.
  import { onMount } from "svelte";
  import { hostedApi } from "../lib/api";

  let { username, password } = $props<{ username: string; password: string }>();

  let providers: any[] = $state([]);
  let agents: any[] = $state([]);
  // Top-level knobs (HPO budgets, …) saved previously — preserved opaquely so
  // a console save never strips what a power user PUT via the API.
  let extraTop: Record<string, any> = $state({});
  let loading = $state(true);
  let saving = $state(false);
  let savedAt = $state("");
  let error = $state("");
  let copied = $state(false);

  const providerByKey = (key: string) => providers.find((p) => p.key === key);

  function starterAgent(): any {
    const p = providerByKey("anthropic") ?? providers[0] ?? {};
    return {
      name: `${p.name_stub ?? "agent"}-1`,
      provider: p.key ?? "",
      model: p.default_model ?? "",
      api_key_env: p.api_key_env ?? "",
      compute: "c3",
      hardware: "auto",
      role: "explorer",
    };
  }

  onMount(async () => {
    try {
      providers = (await hostedApi.providers()).providers ?? [];
      const stored = await hostedApi.contributorConfigGet(username, password);
      if (stored?.config?.agents?.length) {
        const { agents: storedAgents, ...rest } = stored.config;
        agents = storedAgents;
        extraTop = rest;
        savedAt = stored.updated_at ?? "";
      } else {
        agents = [starterAgent()];
      }
    } catch (e: any) {
      error = e.message;
    } finally {
      loading = false;
    }
  });

  async function onProviderChange(agent: any) {
    const p = providerByKey(agent.provider);
    if (!p) return;
    agent.model = p.default_model ?? "";
    agent.api_key_env = p.api_key_env ?? "";
    try {
      const d = await hostedApi.agentDefaults(username, password, agent.provider, agent.model);
      agent.role = d.role;
      if (d.detailed_prompts) agent.detailed_prompts = true;
      else delete agent.detailed_prompts;
    } catch {
      /* defaults are a nicety — keep whatever is set */
    }
  }

  function addAgent() {
    const base = agents[0] ?? starterAgent();
    const stub = providerByKey(base.provider)?.name_stub ?? "agent";
    let n = agents.length + 1;
    while (agents.some((a) => a.name === `${stub}-${n}`)) n += 1;
    agents = [...agents, { ...base, name: `${stub}-${n}` }];
  }

  function removeAgent(i: number) {
    agents = agents.filter((_, idx) => idx !== i);
  }

  const fullConfig = () => ({ ...extraTop, agents });

  async function save() {
    error = ""; saving = true;
    try {
      const res = await hostedApi.contributorConfigPut(username, password, {
        config: fullConfig(),
      });
      savedAt = res.updated_at;
    } catch (e: any) {
      error = e.message;
    } finally {
      saving = false;
    }
  }

  // Complete fleet.config.json — credentials + the plan — so the console is
  // useful before the runner's --join mode lands (P2): download, drop into a
  // clone, run.
  const fleetFileText = () =>
    JSON.stringify(
      { server_url: location.origin, username, swarm_password: password, ...fullConfig() },
      null, 2,
    );

  async function copyFleetFile() {
    await navigator.clipboard.writeText(fleetFileText());
    copied = true;
    setTimeout(() => (copied = false), 1500);
  }

  function downloadFleetFile() {
    const blob = new Blob([fleetFileText()], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "fleet.config.json";
    a.click();
    URL.revokeObjectURL(a.href);
  }
</script>

<div class="card">
  <div class="rowhead">
    <h2>My fleet</h2>
    <div class="spacer"></div>
    {#if savedAt}<span class="muted">saved {savedAt}</span>{/if}
  </div>
  <p class="lede">
    Your fleet plan, stored on the swarm server. Keys stay on your machine —
    agents reference the <em>name</em> of an environment variable
    (e.g. <span class="mono">ANTHROPIC_API_KEY</span>), never the key itself.
  </p>

  {#if error}<div class="banner err">{error}</div>{/if}

  {#if loading}
    <p class="muted">Loading…</p>
  {:else}
    {#each agents as agent, i}
      <div class="agentrow">
        <div class="field">
          <label for={"an" + i}>Name</label>
          <input id={"an" + i} type="text" bind:value={agent.name} />
        </div>
        <div class="field">
          <label for={"ap" + i}>Provider</label>
          <select id={"ap" + i} bind:value={agent.provider} onchange={() => onProviderChange(agent)}>
            {#each providers as p}<option value={p.key}>{p.label}</option>{/each}
          </select>
        </div>
        <div class="field">
          <label for={"am" + i}>Model</label>
          <input id={"am" + i} type="text" bind:value={agent.model} />
        </div>
        <div class="field">
          <label for={"ak" + i}>API key env var</label>
          <input id={"ak" + i} type="text" bind:value={agent.api_key_env} placeholder="OPENROUTER_API_KEY" />
        </div>
        <div class="field">
          <label for={"ac" + i}>Compute</label>
          <select id={"ac" + i} bind:value={agent.compute}>
            <option value="c3">C3 cloud (no Docker)</option>
            <option value="local">Local Docker</option>
          </select>
        </div>
        <div class="field">
          <label for={"ar" + i}>Role</label>
          <select id={"ar" + i} bind:value={agent.role}>
            <option value="explorer">explorer</option>
            <option value="exploiter">exploiter</option>
          </select>
        </div>
        <button class="danger" onclick={() => removeAgent(i)} disabled={agents.length === 1}>✕</button>
      </div>
    {/each}

    <div class="actions" style="margin-top:12px">
      <button class="ghost" onclick={addAgent}>+ Add agent</button>
      <div class="spacer"></div>
      <button class="primary" disabled={saving} onclick={save}>{saving ? "Saving…" : "Save to swarm"}</button>
    </div>

    <div class="field" style="margin-top:20px">
      <label for="dl">Use it now</label>
      <p class="lede" style="margin:4px 0 8px">
        Drop this complete <span class="mono">fleet.config.json</span> into
        your clone and run <span class="mono">python3 run.py</span> — it
        contains your credentials and this plan.
      </p>
      <div class="actions">
        <button class="ghost" onclick={downloadFleetFile}>Download fleet.config.json</button>
        <button class="ghost" onclick={copyFleetFile}>{copied ? "Copied ✓" : "Copy"}</button>
      </div>
    </div>
  {/if}
</div>

<style>
  .agentrow {
    display: grid;
    grid-template-columns: repeat(6, minmax(120px, 1fr)) auto;
    gap: 10px;
    align-items: end;
    padding: 10px 0;
    border-bottom: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  }
  .agentrow .field {
    margin-bottom: 0;
  }
  @media (max-width: 900px) {
    .agentrow {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
