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
  async function recheckPreflight() {
    try { preflight = await localApi.preflight(); } catch { /* keep last */ }
  }
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
    const value = (keyDraft[name] ?? "").trim();
    if (!value) return;
    try {
      secrets = (await localApi.secretSet(name, value)).secrets ?? secrets;
      keyDraft[name] = "";
      keyMsg = `Saved ${name}.`;
    } catch (e: any) {
      if (opts.silent) throw e;
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
{#if keyMsg}<div class="banner ok">{keyMsg}</div>{/if}

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
    <!-- Given room of its own: it answers "am I locked into this?", which is
         the question that stalls people on this step. Cramped under the two
         selects it read as fine print. -->
    <div class="note">
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
          key (the CLI submits the jobs). Run this in a terminal, then Recheck.
          <nav class="tabs" style="margin:10px 0 10px">
            <button class:active={c3Os === "unix"} onclick={() => (c3Os = "unix")}>macOS / Linux</button>
            <button class:active={c3Os === "windows"} onclick={() => (c3Os = "windows")}>Windows</button>
          </nav>
          {#if c3Os === "unix"}
            <CopyCommand text={C3_INSTALL_UNIX} variant="ghost" />
            <div class="hint">
              Then authenticate: <code>c3 login</code>, or
              <code>c3 apikey create tig-swarm</code> and paste the key below.
            </div>
          {:else}
            <div class="hint" style="margin:0 0 8px">
              In <b>PowerShell</b> — the shell installer above is macOS/Linux only:
            </div>
            <CopyCommand text={C3_INSTALL_WIN} variant="ghost" multiline />
            <div class="hint">
              Other open terminals won't see the new <code>PATH</code> until
              restarted. On an ARM Windows machine use
              <code>c3-windows-arm64.exe</code> instead. To update later,
              download again and overwrite <code>c3.exe</code>.
              <br />
              Then authenticate: <code>c3 login</code>, or
              <code>c3 apikey create tig-swarm</code> and paste the key below.
            </div>
          {/if}
          <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
            <button onclick={recheckPreflight}>↻ Recheck</button>
            <span class="hint" style="margin:0">
              Restart this companion after installing if Recheck still
              doesn't see it{#if supportsC3 || dockerInstalled}, or switch to
                <b>Local Docker</b> above{/if}.
            </span>
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
          <div class="row" style="align-items:flex-end">
            <div class="field" style="margin-bottom:0;flex:1">
              <input id="c3k" type="password" bind:value={keyDraft["C3_API_KEY"]}
                placeholder={secrets["C3_API_KEY"]?.set ? "paste new C3_API_KEY to replace it" : "paste C3_API_KEY (stored locally)"} />
            </div>
            <button onclick={() => saveKey("C3_API_KEY")}>{secrets["C3_API_KEY"]?.set ? "Update" : "Save key"}</button>
          </div>
          {#if !secrets["C3_API_KEY"]?.set}
            <div class="hint">
              Create one in
              <a href="https://cthree.cloud/dashboard/settings" target="_blank" rel="noopener">C3 settings</a>
              (sign in at
              <a href="https://cthree.cloud/dashboard/" target="_blank" rel="noopener">cthree.cloud/dashboard</a>).
              Or leave blank and set <code>c3_api_key</code> per agent / use
              <span class="mono">c3 login</span>.
            </div>
          {/if}
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

  /* A text button that reads as the secondary escape hatch it is — the connect
     step's "this isn't the swarm I want" route, which shouldn't compete with
     Continue. */
  .linky {
    background: none; border: none; padding: 0; margin-top: 4px;
    color: var(--ink-dim); font-size: 13.5px; text-decoration: underline;
    cursor: pointer;
  }
  .linky:hover { color: var(--color-accent); }

  .summary { list-style: none; margin-bottom: 8px; }
  .summary li { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border-subtle); font-size: 14px; }
  .summary li span { color: var(--ink-dim); }
</style>
