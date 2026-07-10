<script lang="ts">
  import { onMount } from "svelte";
  import { localApi } from "../lib/api";

  // Direct editor for the actual fleet.config.json — shows the current values
  // and saves them verbatim (preserving fields like api_base / detailed_prompts
  // / hpo knobs the form doesn't surface). This is the "Reconfigure" path from
  // the home fleet card; the step-by-step wizard lives in Contributor.svelte.
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
      const p = providers.find((x: any) => x.key === a.provider);
      if (p?.api_key_env) names.add(p.api_key_env);
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
    } catch (e: any) {
      error = e.message;
    }
  });

  function blankConfig() {
    return { server_url: "", username: "", swarm_password: "", agents: [] };
  }
  function addAgent() {
    // Default model comes from the provider registry (same source the wizard
    // uses) so it tracks scripts/init_fleet.py instead of a hardcoded id.
    const model = providers.find((p) => p.key === "claude-code")?.default_model || "";
    config.agents = [...config.agents, { name: "", provider: "claude-code", model, compute: "local", role: "explorer" }];
  }
  function removeAgent(i: number) {
    config.agents = config.agents.filter((_: any, idx: number) => idx !== i);
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
            <select id={`p${i}`} bind:value={agent.provider}>
              {#each providers as p}<option value={p.key}>{p.key}</option>{/each}
              {#if !providers.some((p) => p.key === agent.provider)}<option value={agent.provider}>{agent.provider}</option>{/if}
            </select>
          </div>
          <div class="field"><label for={`m${i}`}>Model</label><input id={`m${i}`} type="text" bind:value={agent.model} /></div>
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
          {#if agent.api_key_env !== undefined || agent.provider !== "claude-code"}
            <div class="field"><label for={`k${i}`}>API key env</label><input id={`k${i}`} type="text" bind:value={agent.api_key_env} placeholder="e.g. ANTHROPIC_API_KEY" /></div>
          {/if}
        </div>
        {#if agent.api_base !== undefined}
          <div class="field"><label for={`b${i}`}>API base (OpenAI-compatible)</label><input id={`b${i}`} type="text" bind:value={agent.api_base} /></div>
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
    <div class="spacer"></div>
    <button class="primary" disabled={busy || config === null} onclick={save}>{busy ? "Saving…" : "Save configuration"}</button>
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
  .raw { width: 100%; min-height: 360px; font-family: var(--mono); font-size: 12.5px; }
</style>
