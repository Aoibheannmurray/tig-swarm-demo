<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import Stepper from "../components/Stepper.svelte";
  import CopyCommand from "../components/CopyCommand.svelte";
  import { localApi } from "../lib/api";
  import { ensureStream } from "../lib/stream";

  // `onLaunched` is called once the fleet is running. The parent sends the user
  // to the fleet page, so setup ends where fleets are managed from then on —
  // rather than at a monitor embedded in the wizard, which looked like a
  // different feature from the identical one on the fleet page.
  //
  // `prefill` carries credentials handed straight over from the Host screen
  // ("Also run agents yourself"), which self-invites and lands here. It wins
  // over whatever onMount reads off disk.
  let {
    onLaunched = () => {},
    prefill = null,
  }: {
    onLaunched?: () => void;
    prefill?: { server_url?: string; username?: string; swarm_password?: string } | null;
  } = $props();

  const STEPS = ["Connect", "Provider", "Agents", "Tacit", "Launch"];
  let step = $state(0);
  let error = $state("");
  let busy = $state(false);

  // ── Connect ──
  let paste = $state("");
  let serverUrl = $state("");
  let username = $state("");
  let swarmPassword = $state("");

  // Runs on every keystroke/paste into the textarea, and once more from next()
  // as a safety net. It used to need a "Fill fields from paste" button, which
  // people didn't understand they had to press — so they hit Continue with
  // three empty fields and an error.
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

  // Arrived with credentials already in hand (a join link ran `run.py --join`,
  // or the host self-invited). Then Connect is a confirmation, not a form: the
  // fields and the paste box move behind "Use a different swarm".
  let connected = $derived(!!serverUrl && !!username && !!swarmPassword);
  let editConnection = $state(false);

  // ── Provider ──
  let providers: any[] = $state([]);
  let c3hw: any[] = $state([]);
  let provider = $state("claude-code");
  let model = $state("");
  let selectedProvider = $derived(providers.find((p) => p.key === provider));
  $effect(() => {
    if (selectedProvider && model === "") model = selectedProvider.default_model || "";
  });

  // ── Model list ──
  // Two sources, because neither alone is enough: `popular_models` is a short
  // curated shortlist that works with no API key and is the fallback for CLI
  // providers, while the live list is whatever this account or installed Codex
  // CLI can actually call today. `Custom…` keeps any id typeable regardless.
  const CUSTOM = "__custom__";
  let liveModels: string[] = $state([]);
  let modelsError = $state("");
  let modelsLoading = $state(false);
  let customModel = $state(false);
  let fallbackModels = $derived((selectedProvider?.popular_models ?? []) as string[]);
  // The Codex CLI catalog is already curated and priority-ordered by Codex. Use
  // it authoritatively so removed models do not linger in the static fallback.
  let popular = $derived(
    provider === "codex-agentic" && liveModels.length ? liveModels : fallbackModels
  );
  let effectiveDefault = $derived(
    provider === "codex-agentic" && liveModels.length
      ? liveModels[0]
      : selectedProvider?.default_model
  );
  // The live list minus anything already shown under "Recommended".
  let otherModels = $derived(liveModels.filter((m) => !popular.includes(m)));

  async function loadModels(refresh = false) {
    if (!provider) return;
    const requestedProvider = provider;
    modelsLoading = true;
    modelsError = "";
    try {
      const res = await localApi.models(requestedProvider, refresh);
      if (provider !== requestedProvider) return;
      const found = (res.models ?? []) as string[];
      // On first load, follow Codex's current top-priority model rather than a
      // fallback default that may be weeks old. Preserve an explicit choice.
      if (provider === "codex-agentic" && found.length &&
          (model === "" || model === selectedProvider?.default_model || !found.includes(model))) {
        model = found[0];
      }
      liveModels = found;
      modelsError = res.error ?? "";
    } catch (e: any) {
      if (provider !== requestedProvider) return;
      liveModels = [];
      modelsError = e.message;
    } finally {
      if (provider === requestedProvider) modelsLoading = false;
    }
  }

  // Re-fetch whenever the provider changes; a key saved later re-triggers it
  // through the Refresh button (and through saveKey, below).
  $effect(() => {
    provider; // tracked
    liveModels = [];
    modelsError = "";
    customModel = false;
    loadModels();
  });

  // ── Agents ──
  let count = $state(1);
  let prefix = $state("");
  // Default to C3 cloud: it needs no local Docker / Rust toolchain, which is the
  // smoothest path for a non-technical contributor. Falls back to local below
  // when the provider can't use C3.
  let compute = $state("c3");
  let hardware = $state("auto");
  // Behavior picks (both hot-editable later in fleet.config.json):
  // role: how agents edit (explorer = novel rewrites, exploiter = focused
  // tweaks); seeding: where a fresh trajectory starts (working code vs stub).
  // "auto" defers to the tier/server defaults.
  let role = $state("auto");
  let seeding = $state("auto");
  let c3ApiKey = $state("");
  // API keys stored locally in secrets.local.json (no `export` needed).
  // Declared here (before c3Ready reads it) so the rune graph resolves.
  let secrets: Record<string, { set: boolean; source: string }> = $state({});
  let keyDraft: Record<string, string> = $state({});
  let keyMsg = $state("");
  // Whether keyMsg reports a problem. Without it a rejected key ("HTTP 401:
  // invalid x-api-key") rendered in the green success banner.
  let keyMsgBad = $state(false);
  // Unknown until /local-api/providers resolves. Treat unknown as "supported":
  // defaulting to false meant a slow or failed providers fetch silently
  // downgraded the fleet to local Docker and captioned it "This provider runs
  // benchmarks locally (Docker)" — which was never true of any provider.
  let providerKnown = $derived(providers.length > 0 && !!selectedProvider);
  let supportsC3 = $derived(providerKnown ? !!selectedProvider.supports_c3 : true);
  // Keep `compute` valid for the chosen provider: if it can't do C3, force
  // local — but only once we actually know that.
  $effect(() => {
    if (providerKnown && !supportsC3 && compute === "c3") compute = "local";
  });

  // Which C3 install instructions to show. The companion runs on the user's own
  // machine, so the OS it detects is the one they'll install onto.
  type Os = "unix" | "windows";
  let c3Os: Os = $state(
    /win/i.test(navigator.platform || navigator.userAgent || "") ? "windows" : "unix",
  );
  // Transcribed from docs/C3.md — the Windows section (a native binary, no WSL)
  // previously existed only in that doc, while this page claimed the opposite.
  // These are the manual fallback now: the Install/Update button below does the
  // same work through the companion.
  const C3_INSTALL_UNIX = "curl -fsSL https://cthree.cloud/install.sh | sh";
  const C3_INSTALL_WIN = `# Create an install folder.
$dir = "$env:LOCALAPPDATA\\Programs\\c3"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

# Download the Windows binary.
curl.exe -fsSL "https://cthree.cloud/releases/latest/c3-windows-amd64.exe" -o "$dir\\c3.exe"

# Add the folder permanently to the current user's PATH.
$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ';') -notcontains $dir) {
  [System.Environment]::SetEnvironmentVariable("Path", "$userPath;$dir", "User")
}

# Also make it available in this PowerShell window.
$env:Path = "$env:Path;$dir"

c3 --version`;
  // Updating an existing install is just "download over the top" — no folder
  // creation, no PATH edit. Kept separate so the update block isn't 12 lines
  // of steps the user already did.
  const C3_UPDATE_WIN = `$dir = "$env:LOCALAPPDATA\\Programs\\c3"

curl.exe -fsSL "https://cthree.cloud/releases/latest/c3-windows-amd64.exe" \`
  -o "$dir\\c3.exe"

c3 --version`;

  // Capability probe (Docker / C3) so we can guide instead of failing mid-run.
  let preflight: any = $state(null);
  // The `c3` binary is REQUIRED for C3 compute — it performs the deploys; an
  // API key only authenticates it. So "ready" needs the CLI installed, plus
  // some auth source (env key, stored key, key typed here, or `c3 login`).
  const c3CliInstalled = $derived(!!preflight?.c3?.cli_installed);
  const c3HasAuth = $derived(
    !!preflight?.c3?.key_in_env || !!c3ApiKey.trim() || !!secrets["C3_API_KEY"]?.set,
  );
  // `c3 login` sessions aren't detectable, so CLI-installed counts as ready
  // (the banner still nudges toward a key / login); CLI-missing never does.
  const c3Ready = $derived(c3CliInstalled);
  const c3Version = $derived(preflight?.c3?.version ?? null);
  async function recheckPreflight() {
    try { preflight = await localApi.preflight(); } catch { /* keep last */ }
  }

  // ── Install / update the c3 CLI from here ──
  // One endpoint for both: C3 is a young platform shipping new versions
  // constantly, and re-running the installer overwrites the binary in place.
  // Same start-then-poll shape as the Docker install below.
  let c3Install: any = $state(null);
  let c3Poll: ReturnType<typeof setInterval> | null = null;
  function stopC3Poll() {
    if (c3Poll) { clearInterval(c3Poll); c3Poll = null; }
  }
  async function startC3Install() {
    try {
      c3Install = await localApi.c3InstallStart();
    } catch (e: any) {
      c3Install = { state: "error", error: e.message };
      return;
    }
    stopC3Poll();
    c3Poll = setInterval(async () => {
      try {
        c3Install = await localApi.c3InstallStatus();
        if (c3Install.state === "done") {
          stopC3Poll();
          await recheckPreflight();
          c3Install = null;
        } else if (c3Install.state === "error") {
          stopC3Poll();
        }
      } catch { /* companion hiccup — keep polling */ }
    }, 2000);
  }
  onDestroy(stopC3Poll);
  const dockerInstalled = $derived(!!preflight && preflight.docker.installed);
  // Whether the companion can install Docker for them here (Linux + root or
  // passwordless sudo). Everywhere else we show `manual` instead of a button
  // that can't work. Older companions don't send this — treat as unsupported.
  const dockerInstallSupport = $derived(preflight?.docker?.install_support ?? null);

  // ── Install Docker Engine from the UI ──
  // POST runs Docker's convenience script on the companion; we poll until it
  // exits, then re-read preflight (now docker.installed).
  let dockerInstall: any = $state(null);
  let dockerPoll: ReturnType<typeof setInterval> | null = null;
  function stopDockerPoll() {
    if (dockerPoll) { clearInterval(dockerPoll); dockerPoll = null; }
  }
  async function startDockerInstall() {
    try {
      dockerInstall = await localApi.dockerInstallStart();
    } catch (e: any) {
      dockerInstall = { state: "error", error: e.message };
      return;
    }
    stopDockerPoll();
    dockerPoll = setInterval(async () => {
      try {
        dockerInstall = await localApi.dockerInstallStatus();
        if (dockerInstall.state === "done") {
          stopDockerPoll();
          await recheckPreflight();
          // Keep the panel up when a fresh login is still needed — clearing it
          // would imply local compute is ready when the socket is still denied.
          if (dockerInstalled && !dockerInstall.needs_relogin) dockerInstall = null;
        } else if (dockerInstall.state === "error") {
          stopDockerPoll();
        }
      } catch { /* companion hiccup — keep polling */ }
    }, 2000);
  }
  onDestroy(stopDockerPoll);

  // ── API keys (stored locally in secrets.local.json — no `export` needed) ──
  async function refreshSecrets() {
    try { secrets = (await localApi.secretsStatus()).secrets ?? {}; }
    catch { /* companion may predate the endpoint — hide the panel */ }
  }
  // `silent` is the auto-save-on-Continue path: it rethrows so the caller can
  // hold the step, rather than leaving the failure in a banner the step change
  // would scroll past.
  async function saveKey(name: string, opts: { silent?: boolean } = {}) {
    keyMsg = "";
    keyMsgBad = false;
    const value = (keyDraft[name] ?? "").trim();
    if (!value) return;
    try {
      secrets = (await localApi.secretSet(name, value)).secrets ?? secrets;
      keyDraft[name] = "";
      keyMsg = `Saved ${name}.`;
      // The provider's own key is what the live model list needs — fetch it now
      // that one exists, rather than leaving the dropdown on the shortlist.
      if (name === selectedProvider?.api_key_env) {
        if (opts.silent) {
          // Auto-save on Continue: don't make the step change wait on someone
          // else's API.
          loadModels(true);
        } else {
          // Explicit "Set key": report what the key actually bought. A key the
          // provider rejects shows up HERE, at the step that can fix it,
          // instead of at launch.
          keyMsg = `Saved ${name} — loading models…`;
          await loadModels(true);
          keyMsgBad = !!modelsError;
          // modelsError already reads "Could not reach <provider>: …", so it
          // stands as its own sentence rather than being introduced again.
          keyMsg = modelsError
            ? `Saved ${name}. ${modelsError}`
            : `Saved ${name} — ${liveModels.length} models available.`;
        }
      }
    } catch (e: any) {
      if (opts.silent) throw e;
      keyMsgBad = true;
      keyMsg = e.message;
    }
  }
  // The env-var names this fleet will need: the chosen provider's key (if any)
  // plus C3 when benchmarking in the cloud.
  let neededKeys = $derived([
    ...(selectedProvider?.api_key_env ? [selectedProvider.api_key_env] : []),
    ...(compute === "c3" ? ["C3_API_KEY"] : []),
  ]);

  // ── Tacit ──
  // The guided form asks the SAME six prompts as `python setup.py tacit`, and
  // fetches them from the companion rather than restating them here — two
  // copies of an interview drift, and the CLI's is the one the prompt builder
  // was written against. `tacitText` stays as the paste-a-block escape hatch.
  let tacitText = $state("");
  let tacitQuestions: { title: string; hint?: string }[] = $state([]);
  let tacitAnswers: string[] = $state([]);
  // Composed sections, in question order, skipping unanswered prompts — the
  // same shape setup.py's guided capture produces.
  let tacitFilled = $derived(
    tacitQuestions
      .map((q, i) => ({ title: q.title, body: (tacitAnswers[i] ?? "").trim() }))
      .filter((a) => a.body),
  );

  // ── Launch ──
  let writtenConfig: any = $state(null);
  let started = $state(false);

  onMount(async () => {
    try {
      const p = await localApi.providers();
      providers = p.providers;
      c3hw = p.c3_hardware;
      // Best-effort: an older companion has no /tacit/questions, and the step
      // still works as a free-text paste box without them.
      localApi.tacitQuestions()
        .then((t) => { tacitQuestions = t.questions ?? []; })
        .catch(() => {});
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
      // Credentials handed over from the Host screen outrank the file: the host
      // just self-invited, and any config on disk predates that.
      if (prefill) {
        serverUrl = prefill.server_url || serverUrl;
        username = prefill.username || username;
        swarmPassword = prefill.swarm_password || swarmPassword;
      }
    } catch (e: any) {
      error = e.message;
    }
  });

  // Persist any key typed but not explicitly saved. People skipped the "Save
  // key" button and advanced with the field still holding an unsaved value, so
  // the fleet only failed at launch — long after the step that could fix it.
  // Throws on failure so next() can hold the step and surface the reason.
  async function flushKeyDrafts() {
    for (const name of Object.keys(keyDraft)) {
      if ((keyDraft[name] ?? "").trim()) await saveKey(name, { silent: true });
    }
  }

  async function next() {
    error = "";
    if (step === 0) {
      // Covers a paste that never fired an input event (programmatic fill,
      // some mobile keyboards) — cheap, and idempotent when it already ran.
      if (paste.trim()) parsePaste();
      if (!serverUrl || !username || !swarmPassword) {
        error = "server_url, username and swarm_password are all required.";
        return;
      }
    }
    try {
      await flushKeyDrafts();
    } catch (e: any) {
      error = e.message;
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
        role: role === "auto" ? undefined : role,
        seeded_start: seeding === "auto" ? undefined : seeding,
      };
      const res = await localApi.setFleetConfig(params);
      writtenConfig = res.config;
      // A pasted block is the explicit override (its disclosure says so);
      // otherwise send the guided answers, which the server composes into
      // `### <question>` sections exactly like the CLI wizard does.
      if (tacitText.trim()) {
        await localApi.setTacit({ text: tacitText.trim() });
      } else if (tacitFilled.length) {
        await localApi.setTacit({ answers: tacitFilled });
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
      onLaunched();
    } catch (e: any) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

</script>

<Stepper steps={STEPS} current={step} />
{#if error}<div class="banner err">{error}</div>{/if}
{#if keyMsg}<div class={keyMsgBad ? "banner warn" : "banner ok"}>{keyMsg}</div>{/if}

{#if step === 0}
  <div class="card">
    {#if connected && !editConnection}
      <!-- The common case: a join link already wrote these, or the host just
           self-invited. Showing a paste box here made people think they had
           something left to do. -->
      <h2>Connected</h2>
      <ul class="summary">
        <li><span>Username</span><b>{username}</b></li>
        <li><span>Server</span><b>{serverUrl}</b></li>
      </ul>
      <button class="linky" onclick={() => (editConnection = true)}>Use a different swarm</button>
    {:else}
      <h2>Connect to a swarm</h2>
      <p class="lede">Enter the details your host sent you.</p>
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
      <details class="alt">
        <summary>Paste an invite instead</summary>
        <p class="lede" style="margin:10px 0 8px">
          Drop in the lines your host sent — the fields above fill themselves.
        </p>
        <textarea
          id="paste"
          bind:value={paste}
          oninput={parsePaste}
          placeholder={'"server_url": "https://…",\n"username": "your-name",\n"swarm_password": "…"'}
        ></textarea>
      </details>
    {/if}
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
    <!-- The key comes BEFORE the model, because it is what fills the model
         list: asking someone to pick a model first is asking them to choose
         from options that don't exist yet. -->
    {#if selectedProvider?.api_key_env}
      {@const kn = selectedProvider.api_key_env}
      <div class="field">
        <label for="apikey">API key</label>
        <div class="hint" style="margin:0 0 8px">
          Needs <code>{kn}</code>.
          {#if secrets[kn]?.set}
            <span class="pill ok">set ({secrets[kn].source})</span>
          {:else}
            <span class="pill info">not set</span>
          {/if}
          Stored locally in <code>secrets.local.json</code>, never uploaded.
        </div>
        {#if secrets[kn]?.source === "env"}
          <div class="hint">
            Taken from your environment — change it in your shell (a value set
            here would be ignored).
          </div>
        {:else}
          <div class="row" style="align-items:flex-start">
            <div class="field" style="margin-bottom:0;flex:1">
              <input id="apikey" type="password" bind:value={keyDraft[kn]}
                placeholder={secrets[kn]?.set ? `paste a new ${kn} to replace it` : `paste ${kn}`}
                onkeydown={(e) => { if (e.key === "Enter") saveKey(kn); }} />
            </div>
            <div style="flex:0 0 auto">
              <button class="primary" disabled={!(keyDraft[kn] ?? "").trim()}
                onclick={() => saveKey(kn)}>
                {secrets[kn]?.set ? "Update key" : "Set key"}
              </button>
            </div>
          </div>
          <div class="hint">
            Sets the key and loads the models your account can use. Pressing
            Continue saves it too, if you'd rather skip this.
          </div>
        {/if}
      </div>
    {/if}

    <div class="field">
      <label for="model">Model</label>
      {#if customModel}
        <input id="model" type="text" bind:value={model}
          placeholder={selectedProvider?.default_model || "model id"} />
        <div class="hint">
          <button class="linky" onclick={() => { customModel = false; model = popular[0] || ""; }}>
            ← back to the list
          </button>
        </div>
      {:else}
        <select id="model" value={model}
          onchange={(e) => {
            const v = (e.currentTarget as HTMLSelectElement).value;
            if (v === CUSTOM) { customModel = true; } else { model = v; }
          }}>
          {#if popular.length}
            <optgroup label="Recommended">
              {#each popular as m}
                <option value={m}>{m}{m === effectiveDefault ? " — default" : ""}</option>
              {/each}
            </optgroup>
          {/if}
          {#if otherModels.length}
            <optgroup label={`All models on your account (${otherModels.length})`}>
              {#each otherModels as m}<option value={m}>{m}</option>{/each}
            </optgroup>
          {/if}
          <!-- A model the lists don't know (a preview id, a self-hosted
               gateway). Never make the dropdown a dead end. -->
          <optgroup label="Other">
            <option value={CUSTOM}>Custom…</option>
          </optgroup>
        </select>
      {/if}
      <div class="hint" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        {#if modelsLoading}
          <span>Loading this account's models…</span>
        {:else if modelsError}
          <span>{modelsError}</span>
        {:else if provider === "codex-agentic" && liveModels.length}
          <span>{liveModels.length} models available in Codex CLI.</span>
        {:else if otherModels.length}
          <span>{liveModels.length} models available on your account.</span>
        {/if}
        {#if selectedProvider?.api_key_env || provider === "codex-agentic"}
          <button class="linky" onclick={() => loadModels(true)} disabled={modelsLoading}>↻ Refresh list</button>
        {/if}
      </div>
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

    <div class="row">
      <div class="field">
        <label for="role">Agent role</label>
        <select id="role" bind:value={role}>
          <option value="auto">Auto — by model tier (recommended)</option>
          <option value="explorer">Explorer — writes novel, ambitious algorithms</option>
          <option value="exploiter">Exploiter — small focused edits to working code</option>
        </select>
      </div>
      <div class="field">
        <label for="seeding">Starting point</label>
        <select id="seeding" bind:value={seeding}>
          <option value="auto">Auto — server decides (recommended)</option>
          <option value="seed">Seed — start from working code</option>
          <option value="stub">Stub — start from scratch</option>
        </select>
      </div>
    </div>
    <!-- Answers "am I locked into this?", the question that stalls people on
         this step. `bare`: it belongs to the two selects above it, so it sits
         close to them and shares their background rather than reading as a
         separate panel. -->
    <div class="note bare">
      <p>Both can be changed later, while the swarm is running — from
        <b>Reconfigure</b> on your fleet page.</p>
      <p>Changes apply on the agent's next iteration. A new starting point
        takes effect when a fresh trajectory begins.</p>
    </div>

    <!-- Readiness: guide the user to the prerequisites for the chosen backend
         instead of letting the fleet fail mid-run. -->
    {#if compute === "c3"}
      {#if preflight && !c3CliInstalled}
        <div class="banner warn">
          <b>Install the c3 CLI</b> — C3 benchmarking needs it even with an API
          key (the CLI submits the jobs). Install it here, or run it yourself
          and hit Recheck.
          <div style="display:flex;gap:8px;align-items:center;margin:10px 0 4px;flex-wrap:wrap">
            <button class="primary" disabled={c3Install?.state === "pending"} onclick={startC3Install}>
              {c3Install?.state === "pending" ? "Installing…" : "Install c3"}
            </button>
            <button onclick={recheckPreflight}>↻ Recheck</button>
          </div>
          {#if c3Install?.state === "error"}
            <div class="banner err" style="white-space:pre-wrap;margin:8px 0 0">{c3Install.error || "Install failed."}</div>
          {/if}
          {#if c3Install?.output}
            <pre class="mono muted" style="white-space:pre-wrap">{c3Install.output}</pre>
          {/if}
          <details class="alt" style="margin-top:10px">
            <summary>Install it yourself instead</summary>
            <nav class="tabs" style="margin:10px 0">
              <button class:active={c3Os === "unix"} onclick={() => (c3Os = "unix")}>macOS / Linux</button>
              <button class:active={c3Os === "windows"} onclick={() => (c3Os = "windows")}>Windows</button>
            </nav>
            {#if c3Os === "unix"}
              <CopyCommand text={C3_INSTALL_UNIX} />
            {:else}
              <div class="hint" style="margin:0 0 8px">In <b>PowerShell</b>:</div>
              <CopyCommand text={C3_INSTALL_WIN} multiline />
              <div class="hint">
                Other open terminals won't see the new <code>PATH</code> until
                restarted. On an ARM Windows machine use
                <code>c3-windows-arm64.exe</code> instead.
              </div>
            {/if}
          </details>
          <div class="hint" style="margin-top:10px">
            Restart this companion after installing if Recheck still
            doesn't see it{#if supportsC3 || dockerInstalled}, or switch to
              <b>Local Docker</b> above{/if}.
          </div>
        </div>
      {:else if c3Ready}
        <div class="banner ok">
          C3 is ready — {preflight?.c3.key_in_env
            ? "C3_API_KEY detected in your environment."
            : c3HasAuth
              ? "using your saved/entered key."
              : "the c3 CLI is installed (paste a key below, or run c3 login)."}
          No local Docker needed.
        </div>
        <!-- Update path. C3 is young and ships often, and an out-of-date CLI
             fails at deploy time — long after this step — so the button lives
             where the user already is. -->
        <div class="note" style="margin-top:12px">
          <p>
            <b>Already have c3?</b> Update it — C3 is a young platform releasing
            new versions constantly, and an old CLI can fail at deploy time.
            {#if c3Version}<br /><span class="mono muted">{c3Version}</span>{/if}
          </p>
          <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
            <button disabled={c3Install?.state === "pending"} onclick={startC3Install}>
              {c3Install?.state === "pending" ? "Updating…" : "↻ Update c3"}
            </button>
            <button onclick={recheckPreflight}>Recheck version</button>
          </div>
          {#if c3Install?.state === "error"}
            <div class="banner err" style="white-space:pre-wrap;margin:10px 0 0">{c3Install.error || "Update failed."}</div>
          {/if}
          {#if c3Install?.output}
            <pre class="mono muted" style="white-space:pre-wrap">{c3Install.output}</pre>
          {/if}
          <details class="alt" style="margin-top:8px">
            <summary>Update it yourself instead</summary>
            <nav class="tabs" style="margin:10px 0">
              <button class:active={c3Os === "unix"} onclick={() => (c3Os = "unix")}>macOS / Linux</button>
              <button class:active={c3Os === "windows"} onclick={() => (c3Os = "windows")}>Windows</button>
            </nav>
            {#if c3Os === "unix"}
              <CopyCommand text={C3_INSTALL_UNIX} />
            {:else}
              <div class="hint" style="margin:0 0 8px">In <b>PowerShell</b>:</div>
              <CopyCommand text={C3_UPDATE_WIN} multiline />
            {/if}
          </details>
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
          <div class="hint">
            <span class="pill ok">C3_API_KEY set ({secrets["C3_API_KEY"].source})</span>
            {#if secrets["C3_API_KEY"].source === "env"}
              — the environment variable takes precedence; change it in your
              shell (a value saved here would be ignored).
            {/if}
          </div>
        {/if}
        {#if !secrets["C3_API_KEY"]?.set || secrets["C3_API_KEY"]?.source === "file"}
          {#if !secrets["C3_API_KEY"]?.set}
            <!-- Deliberately full-size, not a .hint: getting a C3 key is the
                 one genuinely unfamiliar errand on this page, and it was
                 previously set in the smallest type on the screen. -->
            <div class="note" style="margin:0 0 10px">
              <p>
                <b>Get your key:</b> sign in at
                <a href="https://cthree.cloud/dashboard/" target="_blank" rel="noopener">cthree.cloud/dashboard</a>,
                then create one under
                <a href="https://cthree.cloud/dashboard/settings" target="_blank" rel="noopener">Settings → API keys</a>.
                Paste it below.
              </p>
            </div>
          {/if}
          <input id="c3k" type="password" bind:value={keyDraft["C3_API_KEY"]}
            onchange={() => saveKey("C3_API_KEY")}
            placeholder={secrets["C3_API_KEY"]?.set ? "paste new C3_API_KEY to replace it" : "paste C3_API_KEY (stored locally)"} />
          <div class="hint">
            Saved when you press Continue — stored locally in
            <code>secrets.local.json</code>, never uploaded.
          </div>
        {/if}
      </div>
    {:else if compute === "local"}
      {#if preflight && !dockerInstalled}
        <div class="banner warn">
          Local compute needs Docker, which isn't installed on this machine.
          {#if dockerInstallSupport?.supported}
            <b>Install it here</b> — it takes a minute{#if supportsC3}, or switch to
              <b>C3 cloud</b> above (no Docker needed){/if}.
          {:else}
            {#if supportsC3}Switch to <b>C3 cloud</b> above (no Docker needed), or
              install it yourself:{:else}Install it to continue:{/if}
            <pre class="mono" style="white-space:pre-wrap;margin:8px 0 0">{dockerInstallSupport?.manual ??
              "Install Docker (https://www.docker.com/products/docker-desktop/) and start it."}</pre>
            {#if dockerInstallSupport?.reason}
              <div class="hint">{dockerInstallSupport.reason}</div>
            {/if}
          {/if}
        </div>
        {#if dockerInstallSupport?.supported}
          {#if !dockerInstall || dockerInstall.state === "idle"}
            <button class="primary" onclick={startDockerInstall}>Install Docker</button>
          {:else if dockerInstall.state === "pending"}
            <p class="lede">Installing Docker Engine… this usually takes a minute.</p>
            {#if dockerInstall.output}
              <pre class="mono muted" style="white-space:pre-wrap">{dockerInstall.output}</pre>
            {/if}
          {:else if dockerInstall.state === "error"}
            <div class="banner err" style="white-space:pre-wrap">{dockerInstall.error || "Install failed."}</div>
            {#if dockerInstall.output}
              <pre class="mono muted" style="white-space:pre-wrap">{dockerInstall.output}</pre>
            {/if}
            <button class="primary" onclick={startDockerInstall}>Try again</button>
          {:else if dockerInstall.state === "done"}
            <p class="lede">Installed — rechecking…</p>
          {/if}
        {/if}
      {:else if dockerInstall?.needs_relogin}
        <!-- Engine is in, but this user's `docker` group membership only takes
             effect on a new login — so the fleet still can't reach the socket. -->
        <div class="banner warn">
          Docker is installed, but your user was just added to the
          <span class="mono">docker</span> group — that only takes effect after a
          fresh login. Log out and back in (or reboot), restart this companion,
          then hit Recheck.
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
    {#if tacitQuestions.length}
      <p class="lede">
        Answer whichever prompts you have something for — leave the rest blank.
      </p>
      {#each tacitQuestions as q, i}
        <div class="field tacit-q">
          <label for={`tq${i}`}>{q.title}</label>
          {#if q.hint}<div class="hint" style="margin:0 0 6px">{q.hint}</div>{/if}
          <textarea id={`tq${i}`} bind:value={tacitAnswers[i]} style="min-height:88px"></textarea>
        </div>
      {/each}
      <details class="alt">
        <summary>Paste a block instead</summary>
        <p class="lede" style="margin:10px 0 8px">
          Already have notes written up? Drop them in — they're used instead of
          the answers above.
        </p>
        <textarea id="tk" bind:value={tacitText} style="min-height:140px" placeholder="- When standard local search plateaus, try a large-neighbourhood ruin-and-recreate…"></textarea>
      </details>
    {:else}
      <div class="field">
        <label for="tk">Strategies, heuristics, judgment calls</label>
        <textarea id="tk" bind:value={tacitText} style="min-height:160px" placeholder="- When standard local search plateaus, try a large-neighbourhood ruin-and-recreate…"></textarea>
      </div>
    {/if}
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
      <li><span>Tacit</span><b>{tacitText.trim()
        ? "added (pasted block)"
        : tacitFilled.length
          ? `added (${tacitFilled.length} answer${tacitFilled.length === 1 ? "" : "s"})`
          : "skipped"}</b></li>
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
{/if}

<style>
  /* Explanatory aside — roomier than .hint, which is sized for a one-line
     caption hanging off a single field. */
  .note {
    margin-top: 20px;
    padding: 16px 18px;
    background: var(--bg-sunken);
    border-radius: 6px;
    font-size: 13.5px;
    line-height: 1.6;
    color: var(--ink-mid);
  }
  .note p { margin: 0; }
  .note p + p { margin-top: 10px; }
  .note b { color: var(--ink); font-weight: 600; }
  /* Caption form: text that explains the fields directly above it, not a panel
     of its own. The tinted box and the 20px gap made it look like a separate
     section instead of a footnote to the two selects. */
  .note.bare {
    margin-top: 0;
    padding: 0;
    background: none;
  }

  /* A text button that reads as the secondary escape hatch it is — the connect
     step's "this isn't the swarm I want" route, which shouldn't compete with
     Continue. */
  .linky {
    background: none; border: none; padding: 0; margin-top: 4px;
    color: var(--ink-dim); font-size: 13.5px; text-decoration: underline;
    cursor: pointer;
  }
  .linky:hover { color: var(--color-accent); }

  /* Tacit prompts are full sentences, not field names — the global label style
     (12px, uppercase, letter-spaced) turns a question into a shouted banner
     that's genuinely hard to read. Keep them sentence-case and readable. */
  .tacit-q label {
    text-transform: none;
    letter-spacing: normal;
    font-size: 14.5px;
    color: var(--ink);
    line-height: 1.45;
  }

  .summary { list-style: none; margin-bottom: 8px; }
  .summary li { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border-subtle); font-size: 14px; }
  .summary li span { color: var(--ink-dim); }
</style>
