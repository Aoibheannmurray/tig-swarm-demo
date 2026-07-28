<script lang="ts">
  import { onMount } from "svelte";
  import { localApi } from "../lib/api";
  import { ensureStream } from "../lib/stream";

  // Direct editor for the actual fleet.config.json — shows the current values
  // and saves them verbatim (preserving fields like api_base / detailed_prompts
  // / hpo knobs the form doesn't surface). This is the "Reconfigure" path from
  // the home fleet card; the step-by-step wizard lives in Contributor.svelte.

  // Saving used to be a dead end: a "Saved" banner and no way onward, so the
  // only exit was the masthead's Home link. Both routes land on the fleet page,
  // which is where the monitor and the start/stop controls live.
  let {
    onBack = () => {},
    onStarted = () => {},
  }: { onBack?: () => void; onStarted?: () => void } = $props();
  let config: any = $state(null);
  let providers: any[] = $state([]);
  let c3hw: any[] = $state([]);
  let rawMode = $state(false);
  let rawText = $state("");
  let error = $state("");
  let saved = $state(false);
  let busy = $state(false);
  // "auto" = key absent from the agent entry (defer to tier/server defaults),
  // mirroring the wizard's Auto options. `seeded_start` is stored as a bool
  // in fleet.config.json (true = seed, false = stub — see init_fleet.py).
  const ROLES = ["auto", "explorer", "exploiter"];
  function setRole(agent: any, v: string) {
    if (v === "auto") delete agent.role;
    else agent.role = v;
  }
  const SEEDINGS = [
    ["auto", "auto — server decides"],
    ["seed", "seed — working code"],
    ["stub", "stub — from scratch"],
  ];
  function seedingOf(agent: any): string {
    return agent.seeded_start === true ? "seed" : agent.seeded_start === false ? "stub" : "auto";
  }
  function setSeeding(agent: any, v: string) {
    if (v === "auto") delete agent.seeded_start;
    else agent.seeded_start = v === "seed";
  }

  // ── Providers ──
  //
  // fleet.config.json stores WIRE providers: DeepSeek, OpenRouter and a custom
  // endpoint are all written as `openai` plus an api_base (see _WIRE_REMAP in
  // scripts/init_fleet.py). The setup-level keys the wizard offers are not
  // legal config values — writing `deepseek` straight into an agent entry made
  // the fleet exit at launch with "unknown provider 'deepseek'", which is
  // exactly what this editor used to do. So: show the setup-level choice,
  // recognise it from (provider, api_base), and write the wire shape back.
  function setupKeyOf(agent: any): string {
    const wire = agent?.provider ?? "";
    const base = (agent?.api_base ?? "").trim();
    const exact = providers.find(
      (p) => p.wire_provider === wire && (p.api_base ?? "") === base,
    );
    if (exact) return exact.key;
    // An `openai` entry with an endpoint we don't publish is a custom one.
    if (base) {
      const custom = providers.find((p) => p.needs_api_base && p.wire_provider === wire);
      if (custom) return custom.key;
    }
    return providers.find((p) => p.key === wire)?.key ?? wire;
  }
  function providerOf(agent: any): any {
    return providers.find((p) => p.key === setupKeyOf(agent));
  }
  // Changing the vendor has to carry everything that hangs off it — endpoint,
  // key env var, model, and compute — or the agent ends up a chimera: DeepSeek
  // called at OpenAI's endpoint with a gpt-5 model id and an OPENAI_API_KEY.
  function setProvider(agent: any, key: string, i: number) {
    const p = providers.find((x) => x.key === key);
    if (!p) return;
    agent.provider = p.wire_provider || key;
    if (p.needs_api_base) {
      if (!(agent.api_base ?? "").trim()) agent.api_base = "";
    } else if (p.api_base) {
      agent.api_base = p.api_base;
    } else {
      delete agent.api_base;
    }
    if (p.api_key_env) agent.api_key_env = p.api_key_env;
    else delete agent.api_key_env;
    agent.model = p.default_model || "";
    if (!p.supports_c3 && agent.compute === "c3") agent.compute = "local";
    customModel[i] = false;
    config.agents = [...config.agents];
    loadModels(key, agent);
  }

  // ── Model lists ──
  // Same two sources as the wizard: the provider's curated shortlist (works
  // with no key) plus whatever the account can actually call today, fetched
  // live. `Custom…` keeps any id typeable.
  const CUSTOM_MODEL = "__custom__";
  let liveModels: Record<string, string[]> = $state({});
  let customModel: Record<number, boolean> = $state({});
  const modelsKey = (key: string, agent: any) =>
    providers.find((p) => p.key === key)?.needs_api_base
      ? `${key}|${(agent?.api_base ?? "").trim()}`
      : key;
  async function loadModels(key: string, agent: any) {
    const ck = modelsKey(key, agent);
    if (liveModels[ck]) return;
    const p = providers.find((x) => x.key === key);
    const endpoint = p?.needs_api_base
      ? { api_base: (agent?.api_base ?? "").trim(), api_key_env: agent?.api_key_env }
      : undefined;
    try {
      const res = await localApi.models(key, false, endpoint);
      liveModels[ck] = (res.models ?? []) as string[];
    } catch {
      // No key saved yet, offline, CLI-auth provider — the shortlist stands in.
      liveModels[ck] = [];
    }
  }
  function modelOptions(agent: any): string[] {
    const key = setupKeyOf(agent);
    const p = providers.find((x) => x.key === key);
    const shortlist: string[] = p?.popular_models ?? [];
    const live = liveModels[modelsKey(key, agent)] ?? [];
    const all = [...shortlist, ...live.filter((m) => !shortlist.includes(m))];
    if (agent.model && !all.includes(agent.model)) all.unshift(agent.model);
    return all;
  }
  function setModel(agent: any, i: number, v: string) {
    if (v === CUSTOM_MODEL) customModel[i] = true;
    else agent.model = v;
  }
  // A self-hosted endpoint has no curated shortlist, so until it answers with a
  // catalog the only sane control is a text box — a dropdown whose sole entry
  // is "Custom…" is a dead end dressed as a choice (same rule as the wizard).
  function modelAsText(agent: any, i: number): boolean {
    if (customModel[i]) return true;
    const p = providerOf(agent);
    return !!p?.needs_api_base && modelOptions(agent).length === 0;
  }

  // ── API keys (stored locally in secrets.local.json, not in the config) ──
  let secrets: Record<string, { set: boolean; source: string }> = $state({});
  let keyDraft: Record<string, string> = $state({});
  let keyMsg = $state("");
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
  // Env-var names this fleet needs: each agent provider's key, plus C3 when
  // any agent benchmarks on C3. Same sources as the wizard's neededKeys.
  const neededKeys = $derived.by(() => {
    const names = new Set<string>();
    for (const a of config?.agents ?? []) {
      // The agent's own api_key_env wins: a custom/self-hosted endpoint is
      // written as provider `openai` but keeps its key under whatever name
      // the contributor chose, not OPENAI_API_KEY. Falling back to the
      // SETUP-level provider matters for the same reason — a DeepSeek agent
      // reads as `openai` on the wire but wants DEEPSEEK_API_KEY.
      const env = (a.api_key_env || "").trim() || providerOf(a)?.api_key_env;
      if (env) names.add(env);
      if (a.compute === "c3") names.add("C3_API_KEY");
    }
    return [...names];
  });

  onMount(async () => {
    try {
      const p = await localApi.providers();
      providers = p.providers;
      c3hw = p.c3_hardware;
      refreshSecrets();
      const fc = await localApi.getFleetConfig();
      // Deep clone so edits don't mutate anything until Save.
      config = fc.exists && fc.config ? structuredClone(fc.config) : blankConfig();
      // Warm the model dropdowns for the providers already in use.
      for (const a of config.agents ?? []) loadModels(setupKeyOf(a), a);
    } catch (e: any) {
      error = e.message;
    }
  });

  function blankConfig() {
    return { server_url: "", username: "", swarm_password: "", agents: [] };
  }
  function addAgent() {
    // Model, key env and endpoint all come from the provider registry via
    // setProvider (same source the wizard uses), so a new row is never a
    // half-configured agent.
    const agent: any = { name: "", provider: "claude-code", compute: "local", role: "explorer" };
    config.agents = [...config.agents, agent];
    setProvider(agent, "claude-code", config.agents.length - 1);
  }
  function removeAgent(i: number) {
    config.agents = config.agents.filter((_: any, idx: number) => idx !== i);
    // Indices shift, so the per-row "typing a custom model" flags no longer
    // point at the rows that set them.
    customModel = {};
  }

  function enterRaw() {
    rawText = JSON.stringify(config, null, 2);
    rawMode = true;
    error = "";
  }
  function exitRaw() {
    try {
      config = JSON.parse(rawText);
      rawMode = false;
      error = "";
    } catch (e: any) {
      error = `Invalid JSON: ${e.message}`;
    }
  }

  async function save() {
    busy = true; error = ""; saved = false;
    try {
      if (rawMode) {
        config = JSON.parse(rawText); // throws → caught below
      }
      const res = await localApi.saveFleetConfig(config);
      config = res.config;
      if (rawMode) rawText = JSON.stringify(config, null, 2);
      saved = true;
    } catch (e: any) {
      error = e.message?.includes("JSON") ? `Invalid JSON: ${e.message}` : e.message;
    } finally {
      busy = false;
    }
  }

  async function saveAndStart() {
    await save();
    if (!saved) return; // save() surfaced the reason in `error`
    busy = true; error = "";
    try {
      ensureStream();
      await localApi.fleetStart();
      onStarted();
    } catch (e: any) {
      error = e.message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="card">
  <div class="head">
    <h2>Edit fleet configuration</h2>
    <div class="spacer"></div>
    <button class="ghost" onclick={() => (rawMode ? exitRaw() : enterRaw())}>
      {rawMode ? "Form view" : "Raw JSON"}
    </button>
  </div>
  <p class="lede">Editing <code>fleet.config.json</code> directly. Changes take effect on the next fleet start.</p>

  {#if error}<div class="banner err">{error}</div>{/if}
  {#if saved}<div class="banner ok">Saved fleet.config.json.</div>{/if}

  {#if config === null}
    <p class="muted">Loading…</p>
  {:else if rawMode}
    <textarea class="raw" bind:value={rawText} spellcheck="false"></textarea>
  {:else}
    <div class="row">
      <div class="field"><label for="su">Server URL</label><input id="su" type="text" bind:value={config.server_url} /></div>
    </div>
    <div class="row">
      <div class="field"><label for="un">Username</label><input id="un" type="text" bind:value={config.username} /></div>
      <div class="field"><label for="pw">Swarm password</label><input id="pw" type="password" bind:value={config.swarm_password} /></div>
    </div>

    <div class="agents-head">
      <h3>Agents <span class="muted">({config.agents.length})</span></h3>
      <div class="spacer"></div>
      <button onclick={addAgent}>+ Add agent</button>
    </div>

    {#each config.agents as agent, i}
      <div class="agent">
        <div class="agent-top">
          <div class="field grow"><label for={`n${i}`}>Name</label><input id={`n${i}`} type="text" bind:value={agent.name} /></div>
          <button class="danger sm" onclick={() => removeAgent(i)} disabled={config.agents.length <= 1}>Remove</button>
        </div>
        <div class="row">
          <div class="field">
            <label for={`p${i}`}>Provider</label>
            <select id={`p${i}`} value={setupKeyOf(agent)}
              onchange={(e) => setProvider(agent, e.currentTarget.value, i)}>
              {#each providers as p}<option value={p.key}>{p.label ?? p.key}</option>{/each}
              {#if !providers.some((p) => p.key === setupKeyOf(agent))}
                <option value={setupKeyOf(agent)}>{setupKeyOf(agent)}</option>
              {/if}
            </select>
          </div>
          <div class="field">
            <label for={`m${i}`}>Model</label>
            {#if modelAsText(agent, i)}
              <input id={`m${i}`} type="text" bind:value={agent.model} placeholder="model id" />
              {#if modelOptions(agent).length}
                <button class="linky" onclick={() => (customModel[i] = false)}>use the list</button>
              {/if}
            {:else}
              <select id={`m${i}`} value={agent.model}
                onchange={(e) => setModel(agent, i, e.currentTarget.value)}>
                {#each modelOptions(agent) as m}<option value={m}>{m}</option>{/each}
                <option value={CUSTOM_MODEL}>Custom…</option>
              </select>
            {/if}
          </div>
          <div class="field">
            <label for={`r${i}`}>Role</label>
            <select id={`r${i}`} value={agent.role ?? "auto"} onchange={(e) => setRole(agent, e.currentTarget.value)}>
              {#each ROLES as r}<option value={r}>{r}</option>{/each}
            </select>
          </div>
          <div class="field">
            <label for={`s${i}`}>Starting point</label>
            <select id={`s${i}`} value={seedingOf(agent)} onchange={(e) => setSeeding(agent, e.currentTarget.value)}>
              {#each SEEDINGS as [k, label]}<option value={k}>{label}</option>{/each}
            </select>
          </div>
        </div>
        <div class="row">
          <div class="field">
            <label for={`c${i}`}>Compute</label>
            <select id={`c${i}`} bind:value={agent.compute}>
              <option value="local">local</option>
              <option value="c3">c3</option>
            </select>
          </div>
          {#if agent.compute === "c3"}
            <div class="field">
              <label for={`h${i}`}>C3 hardware</label>
              <select id={`h${i}`} bind:value={agent.hardware}>
                {#each c3hw as h}<option value={h.key}>{h.key}</option>{/each}
              </select>
            </div>
          {/if}
          {#if agent.api_key_env !== undefined || providerOf(agent)?.api_key_env}
            <div class="field"><label for={`k${i}`}>API key env</label><input id={`k${i}`} type="text" bind:value={agent.api_key_env} placeholder="e.g. ANTHROPIC_API_KEY" /></div>
          {/if}
        </div>
        {#if providerOf(agent)?.needs_api_base}
          <div class="field">
            <label for={`b${i}`}>API base (OpenAI-compatible)</label>
            <input id={`b${i}`} type="text" bind:value={agent.api_base}
              placeholder="http://127.0.0.1:8000/v1" />
          </div>
        {:else if agent.api_base}
          <!-- Fixed vendor endpoint, set by the provider choice above. Shown
               so the entry is legible, not editable: hand-editing it is how you
               end up calling one vendor with another's model id. -->
          <p class="muted endpoint">Endpoint <code>{agent.api_base}</code></p>
        {/if}
      </div>
    {/each}

    {#if neededKeys.length}
      <div class="agents-head">
        <h3>API keys</h3>
      </div>
      <p class="lede" style="margin-top:0">
        Stored locally in <code>secrets.local.json</code>, never uploaded —
        separate from the config above, so saving a key takes effect
        immediately.
      </p>
      {#if keyMsg}<div class="banner ok">{keyMsg}</div>{/if}
      {#each neededKeys as kn}
        <div class="field">
          <label for={`key-${kn}`}>{kn}</label>
          {#if secrets[kn]?.set}
            <div class="hint">
              <span class="pill ok">set ({secrets[kn].source})</span>
              {#if secrets[kn].source === "env"}
                — the environment variable takes precedence; change it in your
                shell (a value saved here would be ignored).
              {/if}
            </div>
          {/if}
          {#if !secrets[kn]?.set || secrets[kn]?.source === "file"}
            <div class="row" style="align-items:flex-end">
              <div class="field" style="margin-bottom:0;flex:1">
                <input id={`key-${kn}`} type="password" bind:value={keyDraft[kn]}
                  placeholder={secrets[kn]?.set ? `paste new ${kn} to replace it` : `paste ${kn} (stored locally)`} />
              </div>
              <button onclick={() => saveKey(kn)}>{secrets[kn]?.set ? "Update" : "Save key"}</button>
            </div>
          {/if}
        </div>
      {/each}
    {/if}
  {/if}

  <div class="actions">
    <button onclick={onBack}>← Back to fleet</button>
    <div class="spacer"></div>
    <button disabled={busy || config === null} onclick={save}>{busy ? "Saving…" : "Save configuration"}</button>
    <!-- The usual reason to edit the config is to change how the fleet runs,
         so save-and-run is the primary action, not a second trip via the
         fleet page. -->
    <button class="primary" disabled={busy || config === null} onclick={saveAndStart}>
      {busy ? "Working…" : "Save & start fleet ▶"}
    </button>
  </div>
</div>

<style>
  .head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .head h2 { margin: 0; }
  .agents-head { display: flex; align-items: center; gap: 10px; margin: 22px 0 12px; }
  .agents-head h3 { font-family: var(--display); font-style: italic; font-size: 18px; margin: 0; }
  .agent { border: 1px solid var(--border-default); border-radius: 8px; padding: 16px; margin-bottom: 14px; background: var(--bg-page); }
  .agent-top { display: flex; gap: 12px; align-items: flex-end; }
  .agent-top .grow { flex: 1; }
  .sm { padding: 7px 12px; font-size: 13px; }
  .endpoint { margin: 2px 0 0; font-size: 12px; }
  .linky { background: none; border: 0; padding: 2px 0 0; font-size: 12px;
           color: var(--ink-mid); text-decoration: underline; cursor: pointer; }
  .raw { width: 100%; min-height: 360px; font-family: var(--mono); font-size: 12.5px; }
</style>
