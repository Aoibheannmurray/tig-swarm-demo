<script lang="ts">
  import { onMount } from "svelte";
  import Stepper from "../components/Stepper.svelte";
  import FleetMonitor from "../components/FleetMonitor.svelte";
  import { localApi } from "../lib/api";
  import { ensureStream } from "../lib/stream";

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
  // Default to C3 cloud: it needs no local Docker / Rust toolchain, which is the
  // smoothest path for a non-technical contributor. Falls back to local below
  // when the provider can't use C3.
  let compute = $state("c3");
  let hardware = $state("auto");
  let c3ApiKey = $state("");
  // API keys stored locally in secrets.local.json (no `export` needed).
  // Declared here (before c3Ready reads it) so the rune graph resolves.
  let secrets: Record<string, { set: boolean; source: string }> = $state({});
  let keyDraft: Record<string, string> = $state({});
  let keyMsg = $state("");
  let supportsC3 = $derived(selectedProvider?.supports_c3 ?? false);
  // Keep `compute` valid for the chosen provider: if it can't do C3, force local.
  $effect(() => { if (!supportsC3 && compute === "c3") compute = "local"; });

  // Capability probe (Docker / C3) so we can guide instead of failing mid-run.
  let preflight: any = $state(null);
  const c3Ready = $derived(
    (!!preflight && (preflight.c3.key_in_env || preflight.c3.cli_installed))
      || !!c3ApiKey.trim() || !!secrets["C3_API_KEY"]?.set,
  );
  const dockerInstalled = $derived(!!preflight && preflight.docker.installed);

  // ── API keys (stored locally in secrets.local.json — no `export` needed) ──
  async function refreshSecrets() {
    try { secrets = (await localApi.secretsStatus()).secrets ?? {}; }
    catch { /* companion may predate the endpoint — hide the panel */ }
  }
  async function saveKey(name: string) {
    keyMsg = "";
    const value = (keyDraft[name] ?? "").trim();
    if (!value) return;
    try {
      secrets = (await localApi.secretSet(name, value)).secrets ?? secrets;
      keyDraft[name] = "";
      keyMsg = `Saved ${name}.`;
    } catch (e: any) { keyMsg = e.message; }
  }
  // The env-var names this fleet will need: the chosen provider's key (if any)
  // plus C3 when benchmarking in the cloud.
  let neededKeys = $derived([
    ...(selectedProvider?.api_key_env ? [selectedProvider.api_key_env] : []),
    ...(compute === "c3" ? ["C3_API_KEY"] : []),
  ]);

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
      // Probe capabilities for the readiness panel (non-blocking best-effort).
      localApi.preflight().then((pf) => (preflight = pf)).catch(() => {});
      refreshSecrets();
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

</script>

<Stepper steps={STEPS} current={step} />
{#if error}<div class="banner err">{error}</div>{/if}
{#if keyMsg}<div class="banner ok">{keyMsg}</div>{/if}

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
        {@const kn = selectedProvider.api_key_env}
        <div class="hint">
          Needs <code>{kn}</code>.
          {#if secrets[kn]?.set}
            <span class="pill ok">set ({secrets[kn].source})</span>
          {:else}
            <span class="pill info">not set</span> — paste it below (stored
            locally in <code>secrets.local.json</code>, never uploaded).
          {/if}
        </div>
        {#if !secrets[kn]?.set || secrets[kn]?.source === "file"}
          <div class="row" style="align-items:flex-end;margin-top:8px">
            <div class="field" style="margin-bottom:0;flex:1">
              <input type="password" bind:value={keyDraft[kn]} placeholder={`paste ${kn}`} />
            </div>
            <button onclick={() => saveKey(kn)}>{secrets[kn]?.set ? "Update" : "Save key"}</button>
          </div>
        {/if}
      {/if}
    </div>

    <!-- Login-based providers (claude-code*, codex-agentic) have no api_key_env;
         they use a CLI login. Surface whether that CLI is installed. -->
    {#if selectedProvider && !selectedProvider.api_key_env}
      {@const bin = selectedProvider.key.includes("codex") ? "codex" : "claude"}
      {#if preflight?.clis}
        {#if preflight.clis[bin]}
          <div class="banner ok">
            The <span class="mono">{bin}</span> CLI is installed. If you haven't yet,
            run <span class="mono">{bin} login</span> in your terminal so agents can call it.
          </div>
        {:else}
          <div class="banner warn">
            This provider uses the <span class="mono">{bin}</span> CLI, which isn't
            installed on this machine. Install it and run
            <span class="mono">{bin} login</span>, then relaunch — or pick an API
            provider above and export its key instead.
          </div>
        {/if}
      {/if}
    {/if}
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
        {#if supportsC3}<option value="c3">C3 cloud hardware — recommended, no local setup</option>{/if}
        <option value="local">Local Docker — runs benchmarks on this machine</option>
      </select>
      {#if !supportsC3}<div class="hint">This provider runs benchmarks locally (Docker).</div>{/if}
    </div>

    <!-- Readiness: guide the user to the prerequisites for the chosen backend
         instead of letting the fleet fail mid-run. -->
    {#if compute === "c3"}
      {#if c3Ready}
        <div class="banner ok">
          C3 is ready — {preflight?.c3.key_in_env
            ? "C3_API_KEY detected in your environment."
            : c3ApiKey.trim()
              ? "using the key you entered below."
              : "the c3 CLI is installed (log in with c3 login if you haven't)."}
          No local Docker needed.
        </div>
      {:else if preflight}
        <div class="banner warn">
          C3 isn't set up yet. Paste a C3 API key below, <em>or</em> install the
          c3 CLI (<span class="mono">https://cthree.cloud/install.sh</span>) and run
          <span class="mono">c3 login</span>.
          {#if dockerInstalled}<br />Docker <em>is</em> installed here, so you could
            switch to <b>Local Docker</b> above instead.{/if}
        </div>
      {/if}
      <div class="field">
        <label for="hw">C3 hardware</label>
        <select id="hw" bind:value={hardware}>
          {#each c3hw as h}<option value={h.key}>{h.label}</option>{/each}
        </select>
      </div>
      <div class="field">
        <label for="c3k">C3 API key</label>
        {#if secrets["C3_API_KEY"]?.set}
          <div class="hint"><span class="pill ok">C3_API_KEY set ({secrets["C3_API_KEY"].source})</span></div>
        {:else}
          <div class="row" style="align-items:flex-end">
            <div class="field" style="margin-bottom:0;flex:1">
              <input type="password" bind:value={keyDraft["C3_API_KEY"]}
                placeholder="paste C3_API_KEY (stored locally)" />
            </div>
            <button onclick={() => saveKey("C3_API_KEY")}>Save key</button>
          </div>
          <div class="hint">
            Create one at
            <span class="mono">cthree.cloud/dashboard/settings</span>.
            Or leave blank and set <code>c3_api_key</code> per agent / use
            <span class="mono">c3 login</span>.
          </div>
        {/if}
      </div>
    {:else if compute === "local"}
      {#if preflight && !dockerInstalled}
        <div class="banner warn">
          Local compute needs Docker, which isn't installed on this machine.
          Install Docker Desktop
          (<span class="mono">https://www.docker.com/products/docker-desktop/</span>)
          and start it{#if supportsC3}, or switch to <b>C3 cloud</b> above (no Docker
            needed){/if}.
        </div>
      {:else if preflight && !preflight.docker.running}
        <div class="banner ok">Docker is installed — it'll be started automatically at launch.</div>
      {:else if preflight}
        <div class="banner ok">Docker is installed and running.</div>
      {/if}
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
    <FleetMonitor />
  {/if}
{/if}

<style>
  .summary { list-style: none; margin-bottom: 8px; }
  .summary li { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border-subtle); font-size: 14px; }
  .summary li span { color: var(--ink-dim); }
</style>
