<script lang="ts">
  // The hosted /join page (mode: hosted — served BY the swarm server, like
  // /admin/). A contributor lands here from a one-link invite
  // (`<server>/join#u=<username>&p=<derived-password>`, built by
  // `setup.py invite` / the Admin Console). The credentials ride in the URL
  // fragment so they never reach server logs; we read them client-side,
  // validate them against /api/contributor/me, and hand over ONE per-OS
  // command. This page is deliberately just the doorway — everything after
  // (fleet config, API keys, launch) happens in the LOCAL setup app the
  // command opens, so keys and config never touch the host's server. See
  // docs/server-first-onboarding-plan.md §5.
  import { onMount } from "svelte";
  import Masthead from "../components/Masthead.svelte";
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
  let copied = $state("");

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
  // TEMP: the bootstrap + fleet code live on staging until it merges to
  // main. When merged, set BOOTSTRAP_REF back to "main" (the branch-pinning
  // below then falls away automatically) and drop the /staging raw URL in
  // the README.
  const BOOTSTRAP_REF: string = "staging";
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
  // The secondary path, behind a disclosure: contributors who'd rather have a
  // normal checkout in a directory they chose than the managed one get-swarm.py
  // keeps under their data dir. Same two commands, just run by hand.
  const cloneCmd = () =>
    BOOTSTRAP_REF === "main"
      ? `git clone ${REPO_URL}.git && cd tig-swarm-demo`
      : `git clone -b ${BOOTSTRAP_REF} ${REPO_URL}.git && cd tig-swarm-demo`;
  const runJoinCmd = () =>
    `${osTab === "windows" ? "python" : "python3"} run.py --join "${joinLink()}" --ui`;

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
        {#if me.swarm_name}<b>{me.swarm_name}</b> — optimizing{:else}Optimizing{/if}
        <b>{me.active_challenge}</b>. Your agents appear on the
        <a href="/" target="_blank" rel="noopener">dashboard</a>.
      </p>
    </div>

    <div class="card" style="max-width:640px;margin:16px auto 0">
      <h2>Run agents on your machine</h2>

      <nav class="tabs" style="margin:2px 0 10px">
        <button class:active={osTab === "unix"} onclick={() => (osTab = "unix")}>macOS / Linux</button>
        <button class:active={osTab === "windows"} onclick={() => (osTab = "windows")}>Windows</button>
      </nav>

      <div class="field">
        <div id="boot" class="cmd mono" style="white-space:pre-wrap;word-break:break-all">{bootstrapCmd()}</div>
        <button class="primary" onclick={() => copy(bootstrapCmd(), "boot")}>
          {copied === "boot" ? "Copied ✓" : "Copy command"}
        </button>
      </div>

      <!-- Only what you need before the setup app takes over: what you must
           already have, and what happens when it runs. Everything else lives
           in the README. -->
      <p class="lede" style="margin-top:14px">
        Needs <a href="https://www.python.org/downloads/" target="_blank" rel="noopener">Python 3</a>
        and <a href="https://git-scm.com/downloads" target="_blank" rel="noopener">Git</a>.
        A setup page opens at <span class="mono">127.0.0.1:8787</span> — add your
        API key there and launch.
        {#if osTab === "windows"}
          If <span class="mono">python</span> isn't recognized, try <span class="mono">py</span>.
        {/if}
      </p>

      <details class="alt">
        <summary>Clone the repo instead</summary>
        <ol class="steps">
          <li>
            <div class="cmd mono" style="word-break:break-all">{cloneCmd()}</div>
            <button class="ghost" onclick={() => copy(cloneCmd(), "clone")}>
              {copied === "clone" ? "Copied ✓" : "Copy"}
            </button>
          </li>
          <li>
            <div class="cmd mono" style="white-space:pre-wrap;word-break:break-all">{runJoinCmd()}</div>
            <button class="ghost" onclick={() => copy(runJoinCmd(), "run")}>
              {copied === "run" ? "Copied ✓" : "Copy"}
            </button>
          </li>
        </ol>
      </details>

      <details class="alt">
        <summary>Set up by hand</summary>
        <p class="lede" style="margin:10px 0 8px">
          Put these in <span class="mono">fleet.config.json</span>, then run
          <span class="mono">python3 run.py</span>.
        </p>
        <textarea id="mb" readonly rows="3" aria-label="Credentials for fleet.config.json">{configBlock()}</textarea>
        <button class="ghost" onclick={() => copy(configBlock(), "manual")}>
          {copied === "manual" ? "Copied ✓" : "Copy"}
        </button>
      </details>
    </div>
  {/if}
</div>

<style>
  .steps {
    margin: 10px 0 0;
    padding-left: 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  /* Alternative routes: present, but visually subordinate to the one command
     above them. */
  .alt { margin-top: 10px; }
  .alt summary { cursor: pointer; font-size: 13.5px; color: var(--ink-dim); }
  .alt summary:hover { color: var(--color-accent); }
  .alt[open] summary { margin-bottom: 4px; }
  .cmd {
    background: var(--bg-sunken, rgba(127, 127, 127, 0.12));
    border-radius: 6px;
    padding: 8px 10px;
    margin-bottom: 6px;
    overflow-x: auto;
    font-size: 0.92em;
  }
</style>
