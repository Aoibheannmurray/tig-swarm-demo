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
  import { hostedApi } from "../lib/api";

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
  let tab: "start" | "fleet" | "agents" | "tacit" = $state("start");

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
      <button class:active={tab === "agents"} onclick={() => (tab = "agents")}>My agents</button>
      <button class:active={tab === "tacit"} onclick={() => { tab = "tacit"; loadTacit(); }}>Tacit knowledge</button>
    </nav>

    {#if tab === "fleet"}
      <div style="max-width:900px;margin:0 auto">
        <FleetEditor {username} {password} />
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
        You'll need Python 3 and Git. An LLM API key (Anthropic / OpenAI /
        Google / OpenRouter) pays for your agents' thinking; a
        <a href="https://cthree.cloud/dashboard/settings" target="_blank" rel="noopener">C3
        API key</a> pays for cloud benchmarking (no Docker needed).
      </p>
      <ol class="steps">
        <li>
          <div>Get the swarm code</div>
          <div class="cmd mono">git clone {REPO_URL}.git &amp;&amp; cd tig-swarm-demo</div>
          <button class="ghost" onclick={() => copy(`git clone ${REPO_URL}.git && cd tig-swarm-demo`, "clone")}>
            {copied === "clone" ? "Copied ✓" : "Copy"}
          </button>
        </li>
        <li>
          <div>Copy your credentials — the setup will ask you to paste them</div>
          <div class="cmd mono" style="white-space:pre">{configBlock()}</div>
          <button class="ghost" onclick={() => copy(configBlock(), "creds")}>
            {copied === "creds" ? "Copied ✓" : "Copy"}
          </button>
        </li>
        <li>
          <div>Launch the web setup and follow it</div>
          <div class="cmd mono">python3 run.py --ui</div>
          <button class="ghost" onclick={() => copy("python3 run.py --ui", "run")}>
            {copied === "run" ? "Copied ✓" : "Copy"}
          </button>
        </li>
      </ol>
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
