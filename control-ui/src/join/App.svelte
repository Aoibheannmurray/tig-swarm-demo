<script lang="ts">
  // The hosted /join page (mode: hosted — served BY the swarm server, like
  // /admin/). A contributor lands here from a one-link invite
  // (`<server>/join#u=<username>&p=<derived-password>`, built by
  // `setup.py invite` / the Admin Console). The credentials ride in the URL
  // fragment so they never reach server logs; we read them client-side,
  // validate them against /api/contributor/me, then walk the contributor
  // through getting their fleet running. See
  // docs/server-first-onboarding-plan.md §5.
  import { onMount } from "svelte";
  import Masthead from "../components/Masthead.svelte";
  import FleetEditor from "./FleetEditor.svelte";
  import AgentsPanel from "./AgentsPanel.svelte";
  import CloudRunner from "./CloudRunner.svelte";
  import { hostedApi, buildJoinLink } from "../lib/api";

  // Public repo contributors clone to run a local fleet. Hosts running a
  // fork should update this to point at theirs.
  const REPO_URL = "https://github.com/Aoibheannmurray/tig-swarm-demo";

  const CREDS_KEY = "prom_join_creds";

  let phase: "checking" | "ok" | "bad" | "nolink" = $state("checking");
  let error = $state("");
  let me: any = $state(null);
  let username = $state("");
  let password = $state("");
  let showManual = $state(false);
  let copied = $state("");
  let tab: "start" | "fleet" | "cloud" | "agents" | "tacit" = $state("start");

  // ── Tacit knowledge (stored server-side alongside the fleet config) ──
  let tacit = $state("");
  let tacitLoaded = $state(false);
  let tacitSaving = $state(false);
  let tacitMsg = $state("");

  async function loadTacit() {
    if (tacitLoaded) return;
    try {
      const stored = await hostedApi.contributorConfigGet(username, password);
      tacit = stored?.tacit ?? "";
    } catch {
      /* leave empty — saving still works */
    }
    tacitLoaded = true;
  }

  async function saveTacit() {
    tacitMsg = ""; tacitSaving = true;
    try {
      await hostedApi.contributorConfigPut(username, password, { tacit });
      tacitMsg = "Saved.";
    } catch (e: any) {
      tacitMsg = e.message;
    } finally {
      tacitSaving = false;
    }
  }

  onMount(async () => {
    const frag = new URLSearchParams(location.hash.slice(1));
    username = frag.get("u") ?? "";
    password = frag.get("p") ?? "";
    // Scrub the credentials from the address bar (screenshots, shoulder
    // surfing, browser history). They live in component state +
    // localStorage from here on.
    if (location.hash) {
      history.replaceState(null, "", location.pathname + location.search);
    }
    if (!username || !password) {
      // Returning visitor without a fragment: fall back to saved creds.
      try {
        const saved = JSON.parse(localStorage.getItem(CREDS_KEY) ?? "null");
        if (saved?.username && saved?.password) {
          username = saved.username;
          password = saved.password;
        }
      } catch {
        /* corrupted saved creds — treat as absent */
      }
    }
    if (!username || !password) {
      phase = "nolink";
      return;
    }
    try {
      me = await hostedApi.contributorMe(username, password);
      localStorage.setItem(CREDS_KEY, JSON.stringify({ username, password }));
      phase = "ok";
    } catch (e: any) {
      error = e.message;
      phase = "bad";
    }
  });

  // This page is served by the swarm server itself, so its origin IS the
  // server URL contributors must configure.
  const serverUrl = () => location.origin;
  const configBlock = () =>
    `"server_url": "${serverUrl()}",\n"username": "${username}",\n"swarm_password": "${password}"`;

  // Rebuild the join link from the validated credentials (the URL fragment was
  // scrubbed on load) so it can be baked into copy-paste commands. Raw base is
  // derived from REPO_URL so a fork's own bootstrap URL works.
  const RAW_BASE = REPO_URL.replace("github.com", "raw.githubusercontent.com");
  // TEMP: the bootstrap + fleet code live on this branch until it merges to
  // main. When merged, set BOOTSTRAP_REF back to "main" (the branch-pinning
  // below then falls away automatically) and drop the /server-onboarding raw
  // URL in the README.
  const BOOTSTRAP_REF: string = "server-onboarding";
  const joinLink = () => buildJoinLink(serverUrl(), username, password);

  // ── Per-OS one-liner ──
  // The primary flow: one command fetches the swarm code and opens the local
  // setup app (run.py --join <link> --ui), where the contributor picks
  // provider/model, pastes their API keys (stored on THEIR machine, never
  // sent to this server), and clicks Launch fleet. Windows ships curl.exe and
  // uses `python`; the --branch flag (handled by get-swarm.py) replaces
  // env-var syntax so one command shape works in every shell.
  let osTab: "unix" | "windows" = $state(
    /win/i.test(navigator.platform || navigator.userAgent || "") ? "windows" : "unix",
  );
  const branchFlag = BOOTSTRAP_REF === "main" ? "" : ` --branch ${BOOTSTRAP_REF}`;
  const bootstrapCmd = () => {
    const curl = osTab === "windows" ? "curl.exe" : "curl";
    const py = osTab === "windows" ? "python" : "python3";
    return (
      `${curl} -fsSL ${RAW_BASE}/${BOOTSTRAP_REF}/deploy/get-swarm.py | ` +
      `${py} - join "${joinLink()}" --ui${branchFlag}`
    );
  };
  const cloneCmd = () =>
    BOOTSTRAP_REF === "main"
      ? `git clone ${REPO_URL}.git && cd tig-swarm-demo`
      : `git clone -b ${BOOTSTRAP_REF} ${REPO_URL}.git && cd tig-swarm-demo`;
  const runJoinCmd = () =>
    `${osTab === "windows" ? "python" : "python3"} run.py --join "${joinLink()}" --ui`;
  const dockerCmd = () =>
    `docker run --rm -e TIG_JOIN_LINK="${joinLink()}" ` +
    `-e OPENROUTER_API_KEY=sk-… -e C3_API_KEY=c3-… ` +
    `ghcr.io/Aoibheannmurray/tig-swarm-contributor`;

  async function copy(text: string, tag: string) {
    await navigator.clipboard.writeText(text);
    copied = tag;
    setTimeout(() => (copied = ""), 1500);
  }

  function forget() {
    localStorage.removeItem(CREDS_KEY);
    username = "";
    password = "";
    me = null;
    phase = "nolink";
  }
</script>

<div class="shell">
  <Masthead title="Prometheus" subtitle="Join the swarm">
    {#if phase === "ok"}<button class="ghost" onclick={forget}>Forget me on this device</button>{/if}
  </Masthead>

  {#if phase === "checking"}
    <div class="card" style="max-width:560px;margin:0 auto">
      <h2>Checking your invite…</h2>
    </div>
  {:else if phase === "nolink"}
    <div class="card" style="max-width:560px;margin:0 auto">
      <h2>You need a join link</h2>
      <p class="lede">
        This page turns a host's invite into a running fleet. Ask the swarm
        host for your <b>join link</b> — it looks like
        <span class="mono">{location.origin}/join#u=…</span> and carries your
        personal credentials.
      </p>
    </div>
  {:else if phase === "bad"}
    <div class="card" style="max-width:560px;margin:0 auto">
      <h2>Invite not valid</h2>
      <div class="banner err">{error}</div>
      <p class="lede">
        The link may be mistyped, superseded, or your access was revoked. Ask
        the host for a fresh join link.
      </p>
    </div>
  {:else}
    <div class="card" style="max-width:640px;margin:0 auto">
      <h2>✓ Valid invite for <span class="mono">{me.username}</span></h2>
      <p class="lede">
        {#if me.swarm_name}This swarm is <b>{me.swarm_name}</b> — currently
        optimizing <b>{me.active_challenge}</b> ({me.swarm_type} challenges).
        {:else}This swarm is currently optimizing
        <b>{me.active_challenge}</b> ({me.swarm_type} challenges).{/if}
        Your agents will appear on the
        <a href="/" target="_blank" rel="noopener">dashboard</a> under
        <span class="mono">{me.username}</span>.
      </p>
    </div>

    <nav class="tabs" style="max-width:640px;margin:16px auto 0">
      <button class:active={tab === "start"} onclick={() => (tab = "start")}>Get started</button>
      <button class:active={tab === "fleet"} onclick={() => (tab = "fleet")}>My fleet</button>
      {#if me.runner_url}
        <button class:active={tab === "cloud"} onclick={() => (tab = "cloud")}>Run in the cloud</button>
      {/if}
      <button class:active={tab === "agents"} onclick={() => (tab = "agents")}>My agents</button>
      <button class:active={tab === "tacit"} onclick={() => { tab = "tacit"; loadTacit(); }}>Tacit knowledge</button>
    </nav>

    {#if tab === "fleet"}
      <div style="max-width:900px;margin:0 auto">
        <FleetEditor {username} {password} />
      </div>
    {:else if tab === "cloud" && me.runner_url}
      <div style="max-width:640px;margin:0 auto">
        <CloudRunner runnerUrl={me.runner_url} {username} {password} />
      </div>
    {:else if tab === "agents"}
      <div style="max-width:760px;margin:0 auto">
        <AgentsPanel {username} {password} />
      </div>
    {:else if tab === "tacit"}
      <div class="card" style="max-width:640px;margin:0 auto">
        <h2>Tacit knowledge</h2>
        <p class="lede">
          Hints your agents see when they stagnate — strategies to try, dead
          ends to avoid. Stored with your fleet plan on the swarm server.
        </p>
        <div class="field">
          <label for="tk">Notes (one hint per line, `- ` bullets)</label>
          <textarea id="tk" rows="10" bind:value={tacit}
            placeholder="- try simulated annealing before giving up on a neighborhood"></textarea>
        </div>
        <div class="actions">
          {#if tacitMsg}<span class="muted">{tacitMsg}</span>{/if}
          <div class="spacer"></div>
          <button class="primary" disabled={tacitSaving} onclick={saveTacit}>
            {tacitSaving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    {/if}

    <div class="card" style="max-width:640px;margin:16px auto 0"
         hidden={tab !== "start"}>
      <h2>Run agents on your machine</h2>
      <p class="lede">
        One command sets everything up: it fetches the swarm code, then opens a
        <b>setup app in your browser</b> — pick your AI provider and models,
        paste your API keys there (an LLM key, and a
        <a href="https://cthree.cloud/dashboard/settings" target="_blank" rel="noopener">C3
        key</a> for cloud benchmarking), and click <b>Launch fleet</b>. Keys
        stay on your machine.
      </p>
      <p class="lede">
        You need <a href="https://www.python.org/downloads/" target="_blank" rel="noopener">Python 3</a>
        and <a href="https://git-scm.com/downloads" target="_blank" rel="noopener">Git</a>
        installed (macOS offers Git automatically the first time you use it).
      </p>

      <nav class="tabs" style="margin:4px 0 10px">
        <button class:active={osTab === "unix"} onclick={() => (osTab = "unix")}>macOS / Linux</button>
        <button class:active={osTab === "windows"} onclick={() => (osTab = "windows")}>Windows</button>
      </nav>

      <div class="field">
        <label for="boot">Paste this into a terminal{osTab === "windows" ? " (PowerShell or cmd)" : ""}</label>
        <div id="boot" class="cmd mono" style="white-space:pre-wrap;word-break:break-all">{bootstrapCmd()}</div>
        <button class="ghost" onclick={() => copy(bootstrapCmd(), "boot")}>
          {copied === "boot" ? "Copied ✓" : "Copy command"}
        </button>
        {#if osTab === "windows"}
          <div class="hint">If <span class="mono">python</span> isn't recognized, try <span class="mono">py</span> instead.</div>
        {/if}
      </div>
      <p class="lede" style="margin-top:6px">
        Your join link and credentials are already in the command — the setup
        app opens pre-filled at <span class="mono">http://127.0.0.1:8787</span>.
      </p>

      <details style="margin-top:12px">
        <summary>Prefer to clone the repo yourself?</summary>
        <ol class="steps" style="margin-top:10px">
          <li>
            <div>Get the code</div>
            <div class="cmd mono" style="word-break:break-all">{cloneCmd()}</div>
            <button class="ghost" onclick={() => copy(cloneCmd(), "clone")}>
              {copied === "clone" ? "Copied ✓" : "Copy"}
            </button>
          </li>
          <li>
            <div>Open the setup app with your join link</div>
            <div class="cmd mono" style="white-space:pre-wrap;word-break:break-all">{runJoinCmd()}</div>
            <button class="ghost" onclick={() => copy(runJoinCmd(), "run")}>
              {copied === "run" ? "Copied ✓" : "Copy"}
            </button>
          </li>
        </ol>
      </details>

      <details style="margin-top:8px">
        <summary>Run headless in Docker instead</summary>
        <p class="lede" style="margin-top:8px">
          No setup app: configure your agents in the <b>My fleet</b> tab first,
          and pass your keys as environment variables (a container can't prompt):
        </p>
        <div class="cmd mono" style="white-space:pre-wrap;word-break:break-all">{dockerCmd()}</div>
        <button class="ghost" onclick={() => copy(dockerCmd(), "docker")}>
          {copied === "docker" ? "Copied ✓" : "Copy"}
        </button>
      </details>
    </div>

    <div class="card" style="max-width:640px;margin:16px auto 0"
         hidden={tab !== "start"}>
      <button class="ghost" onclick={() => (showManual = !showManual)}>
        {showManual ? "▾" : "▸"} Manual / power-user flow
      </button>
      {#if showManual}
        <p class="lede" style="margin-top:12px">
          Paste these into <span class="mono">fleet.config.json</span>
          (replacing the matching keys), then run
          <span class="mono">python3 run.py</span> for the terminal wizard —
          or <span class="mono">python3 scripts/run_fleet.py</span> if your
          config is already complete.
        </p>
        <div class="field">
          <label for="mb">Credentials</label>
          <textarea id="mb" readonly rows="3">{configBlock()}</textarea>
          <button class="ghost" onclick={() => copy(configBlock(), "manual")}>
            {copied === "manual" ? "Copied ✓" : "Copy"}
          </button>
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .steps {
    margin: 8px 0 0;
    padding-left: 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .steps li > div:first-child {
    margin-bottom: 6px;
  }
  .cmd {
    background: var(--bg-sunken, rgba(127, 127, 127, 0.12));
    border-radius: 6px;
    padding: 8px 10px;
    margin-bottom: 6px;
    overflow-x: auto;
    font-size: 0.92em;
  }
</style>
