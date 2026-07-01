<script lang="ts">
  import { onMount } from "svelte";
  import Stepper from "../components/Stepper.svelte";
  import LogStream from "../components/LogStream.svelte";
  import { localApi } from "../lib/api";
  import { ensureStream, fleetLog, fleetStatus } from "../lib/stream";

  const STEPS = ["Connect", "Provider", "Agents", "Tacit", "Launch"];
  let step = $state(0);
  let error = $state("");
  let busy = $state(false);

  // ── Connect ──
  let paste = $state("");
  let serverUrl = $state("");
  let username = $state("");
  let swarmPassword = $state("");

  function parsePaste() {
    const grab = (k: string) => {
      const m =
        paste.match(new RegExp(`["']?${k}["']?\\s*[:=]\\s*"([^"]+)"`)) ||
        paste.match(new RegExp(`\\b${k}\\s*[:=]\\s*([^\\s,}]+)`));
      return m ? m[1].trim() : "";
    };
    serverUrl = grab("server_url") || serverUrl;
    username = grab("username") || username;
    swarmPassword = grab("swarm_password") || swarmPassword;
  }

  // ── Provider ──
  let providers: any[] = $state([]);
  let c3hw: any[] = $state([]);
  let provider = $state("claude-code");
  let model = $state("");
  let selectedProvider = $derived(providers.find((p) => p.key === provider));
  $effect(() => {
    if (selectedProvider && model === "") model = selectedProvider.default_model || "";
  });

  // ── Agents ──
  let count = $state(1);
  let prefix = $state("");
  let compute = $state("local");
  let hardware = $state("auto");
  let c3ApiKey = $state("");
  let supportsC3 = $derived(selectedProvider?.supports_c3 ?? false);

  // ── Tacit ──
  let tacitText = $state("");

  // ── Launch ──
  let writtenConfig: any = $state(null);
  let started = $state(false);

  onMount(async () => {
    try {
      const p = await localApi.providers();
      providers = p.providers;
      c3hw = p.c3_hardware;
      // Prefill connection from an existing fleet.config.json, if any.
      const fc = await localApi.getFleetConfig();
      if (fc.exists && fc.config) {
        serverUrl = fc.config.server_url ?? "";
        username = fc.config.username ?? "";
        swarmPassword = fc.config.swarm_password ?? "";
      }
    } catch (e: any) {
      error = e.message;
    }
  });

  function next() {
    error = "";
    if (step === 0 && (!serverUrl || !username || !swarmPassword)) {
      error = "server_url, username and swarm_password are all required.";
      return;
    }
    step = Math.min(step + 1, STEPS.length - 1);
  }
  function back() { error = ""; step = Math.max(step - 1, 0); }

  async function saveConfigAndTacit() {
    busy = true; error = "";
    try {
      const params: any = {
        server_url: serverUrl, username, swarm_password: swarmPassword,
        provider, model, count, prefix: prefix || undefined,
        compute, hardware: compute === "c3" ? hardware : undefined,
        c3_api_key: compute === "c3" ? c3ApiKey : undefined,
      };
      const res = await localApi.setFleetConfig(params);
      writtenConfig = res.config;
      if (tacitText.trim()) {
        await localApi.setTacit({ text: tacitText.trim() });
      }
    } catch (e: any) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function startFleet() {
    busy = true; error = "";
    try {
      ensureStream();
      await localApi.fleetStart();
      started = true;
    } catch (e: any) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function stopFleet() {
    try { await localApi.fleetStop(); } catch (e: any) { error = e.message; }
  }
</script>

<Stepper steps={STEPS} current={step} />
{#if error}<div class="banner err">{error}</div>{/if}

{#if step === 0}
  <div class="card">
    <h2>Connect to a swarm</h2>
    <p class="lede">Paste the lines your host sent you, or type them in.</p>
    <div class="field">
      <label for="paste">Paste invite</label>
      <textarea id="paste" bind:value={paste} placeholder={'"server_url": "https://…",\n"username": "your-name",\n"swarm_password": "…"'}></textarea>
      <button class="ghost" onclick={parsePaste}>Fill fields from paste</button>
    </div>
    <div class="field">
      <label for="su">Server URL</label>
      <input id="su" type="text" bind:value={serverUrl} placeholder="https://my-swarm.up.railway.app" />
    </div>
    <div class="row">
      <div class="field">
        <label for="un">Username</label>
        <input id="un" type="text" bind:value={username} />
      </div>
      <div class="field">
        <label for="pw">Swarm password</label>
        <input id="pw" type="password" bind:value={swarmPassword} />
      </div>
    </div>
    <div class="actions"><div class="spacer"></div><button class="primary" onclick={next}>Continue →</button></div>
  </div>
{:else if step === 1}
  <div class="card">
    <h2>Choose your LLM</h2>
    <p class="lede">Which model should your agents call?</p>
    <div class="field">
      <label for="prov">Provider</label>
      <select id="prov" bind:value={provider} onchange={() => (model = "")}>
        {#each providers as p}<option value={p.key}>{p.label}</option>{/each}
      </select>
      {#if selectedProvider}<div class="hint">{selectedProvider.blurb}</div>{/if}
    </div>
    <div class="field">
      <label for="model">Model</label>
      <input id="model" type="text" bind:value={model} placeholder={selectedProvider?.default_model || "model id"} />
      {#if selectedProvider?.api_key_env}
        <div class="hint">Needs <code>{selectedProvider.api_key_env}</code> exported in the shell that runs the fleet.</div>
      {/if}
    </div>
    <div class="actions"><button onclick={back}>← Back</button><div class="spacer"></div><button class="primary" onclick={next}>Continue →</button></div>
  </div>
{:else if step === 2}
  <div class="card">
    <h2>Agents &amp; compute</h2>
    <div class="row">
      <div class="field">
        <label for="count">How many agents</label>
        <input id="count" type="number" min="1" bind:value={count} />
      </div>
      <div class="field">
        <label for="prefix">Name prefix (optional)</label>
        <input id="prefix" type="text" bind:value={prefix} placeholder="auto-generated if blank" />
      </div>
    </div>
    <div class="field">
      <label for="compute">Compute backend</label>
      <select id="compute" bind:value={compute} disabled={!supportsC3}>
        <option value="local">Local Docker — runs benchmarks on this machine</option>
        {#if supportsC3}<option value="c3">C3 cloud hardware — runs benchmarks remotely</option>{/if}
      </select>
      {#if !supportsC3}<div class="hint">This provider runs benchmarks locally.</div>{/if}
    </div>
    {#if compute === "c3"}
      <div class="field">
        <label for="hw">C3 hardware</label>
        <select id="hw" bind:value={hardware}>
          {#each c3hw as h}<option value={h.key}>{h.label}</option>{/each}
        </select>
      </div>
      <div class="field">
        <label for="c3k">C3 API key (optional)</label>
        <input id="c3k" type="password" bind:value={c3ApiKey} placeholder="leave blank to use C3_API_KEY / c3 login" />
      </div>
    {/if}
    <div class="actions"><button onclick={back}>← Back</button><div class="spacer"></div><button class="primary" onclick={next}>Continue →</button></div>
  </div>
{:else if step === 3}
  <div class="card">
    <h2>Tacit knowledge <span class="muted" style="font-size:14px">(optional)</span></h2>
    <p class="lede">
      Private hints your agents consult when they stagnate. Never uploaded or
      shared across the swarm. Skip and add later any time.
    </p>
    <div class="field">
      <label for="tk">Strategies, heuristics, judgment calls</label>
      <textarea id="tk" bind:value={tacitText} style="min-height:160px" placeholder="- When standard local search plateaus, try a large-neighbourhood ruin-and-recreate…"></textarea>
    </div>
    <div class="actions"><button onclick={back}>← Back</button><div class="spacer"></div><button class="primary" onclick={next}>Review →</button></div>
  </div>
{:else if step === 4}
  <div class="card">
    <h2>Review &amp; launch</h2>
    <ul class="summary">
      <li><span>Server</span><b>{serverUrl}</b></li>
      <li><span>Username</span><b>{username}</b></li>
      <li><span>Provider</span><b>{provider}{model ? ` · ${model}` : ""}</b></li>
      <li><span>Agents</span><b>{count}{prefix ? ` · ${prefix}-*` : ""}</b></li>
      <li><span>Compute</span><b>{compute === "c3" ? `c3 / ${hardware}` : "local"}</b></li>
      <li><span>Tacit</span><b>{tacitText.trim() ? "added" : "skipped"}</b></li>
    </ul>

    {#if !writtenConfig}
      <div class="actions">
        <button onclick={back}>← Back</button><div class="spacer"></div>
        <button class="primary" disabled={busy} onclick={saveConfigAndTacit}>
          {busy ? "Saving…" : "Save fleet.config.json"}
        </button>
      </div>
    {:else}
      <div class="banner ok">Saved fleet.config.json — {writtenConfig.agents.map((a: any) => a.name).join(", ")}</div>
      {#if !started}
        <div class="actions">
          <div class="spacer"></div>
          <button class="primary" disabled={busy} onclick={startFleet}>{busy ? "Starting…" : "▶ Start fleet"}</button>
        </div>
      {/if}
    {/if}
  </div>

  {#if started}
    <div class="card">
      <div class="monitor-head">
        <h2>Fleet monitor</h2>
        <div class="spacer"></div>
        <span class="pill {$fleetStatus.state === 'running' ? 'ok' : $fleetStatus.state === 'error' ? 'err' : 'info'}">{$fleetStatus.state}</span>
        <button class="danger" onclick={stopFleet}>■ Stop</button>
      </div>
      <div class="agentgrid">
        {#each Object.entries($fleetStatus.agents || {}) as [name, a]}
          <div class="agentcard">
            <b>{name}</b>
            <span class="pill {(a as any).state === 'running' ? 'ok' : 'info'}">{(a as any).state}</span>
            {#if (a as any).pid}<span class="muted mono">pid {(a as any).pid}</span>{/if}
          </div>
        {/each}
      </div>
      <LogStream lines={$fleetLog} height="360px" />
    </div>
  {/if}
{/if}

<style>
  .summary { list-style: none; margin-bottom: 8px; }
  .summary li { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border-subtle); font-size: 14px; }
  .summary li span { color: var(--ink-dim); }
  .monitor-head { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
  .agentgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .agentcard { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: var(--bg-page); border: 1px solid var(--border-subtle); border-radius: 6px; font-size: 13px; }
  .agentcard b { flex: 1; }
</style>
