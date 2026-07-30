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
  import CopyCommand from "../components/CopyCommand.svelte";
  import { hostedApi, buildJoinLink } from "../lib/api";

  // Public repo contributors clone to run a local fleet. Hosts running a
  // fork should update this to point at theirs.
  const REPO_URL = "https://github.com/tig-foundation/prometheus-early-beta";

  const CREDS_KEY = "prom_join_creds";

  let phase: "checking" | "ok" | "bad" | "nolink" = $state("checking");
  let error = $state("");
  let me: any = $state(null);
  let username = $state("");
  let password = $state("");

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
  // The public repo's main branch carries the bootstrap + fleet code, so the
  // commands below need no branch pinning (set this to a branch name to pin
  // both the clone and the raw bootstrap URL to it).
  const BOOTSTRAP_REF: string = "main";
  const joinLink = () => buildJoinLink(serverUrl(), username, password);

  // ── Per-OS commands ──
  // macOS and Linux are split so each can name its own way to open a terminal;
  // the command text only differs between unix-likes and Windows.
  type Os = "mac" | "linux" | "windows";
  function detectOs(): Os {
    const s = navigator.platform || navigator.userAgent || "";
    if (/win/i.test(s)) return "windows";
    if (/mac|iphone|ipad/i.test(s)) return "mac";
    return "linux";
  }
  let osTab: Os = $state(detectOs());
  const isWin = $derived(osTab === "windows");

  const branchFlag = BOOTSTRAP_REF === "main" ? "" : ` --branch ${BOOTSTRAP_REF}`;
  // `py` (the Python launcher) rather than `python` on Windows: a machine with
  // no python.org install resolves `python` to the Microsoft Store alias stub,
  // which in a non-interactive pipe opens the Store and exits silently — see
  // the note on the one-liner below. `py` only exists with a real install.
  const pyBin = $derived(isWin ? "py" : "python3");
  const bootstrapCmd = () =>
    `${isWin ? "curl.exe" : "curl"} -fsSL ${RAW_BASE}/${BOOTSTRAP_REF}/deploy/get-swarm.py | ` +
    `${pyBin} - join "${joinLink()}" --ui${branchFlag}`;
  // Two commands, joined per-OS. `&&` is a syntax error in Windows PowerShell
  // 5.1 — still the default shell on a stock Windows — so there the two
  // commands go on their own lines instead (pasting a two-line block runs both).
  const cloneArgs = () =>
    BOOTSTRAP_REF === "main"
      ? `git clone ${REPO_URL}.git`
      : `git clone -b ${BOOTSTRAP_REF} ${REPO_URL}.git`;
  const cloneCmd = () =>
    isWin
      ? `${cloneArgs()}\ncd prometheus-early-beta`
      : `${cloneArgs()} && cd prometheus-early-beta`;
  const runJoinCmd = () => `${pyBin} run.py --join "${joinLink()}" --ui`;

  // How to get to a terminal in the first place — the step the page used to
  // skip entirely. A web page can't open one (no browser API exists), so this
  // is the honest substitute. Windows says "search for it": Win + X is a
  // keyboard chord people miss, and what the menu is called moves between
  // Windows versions ("Terminal" on 11, "Windows PowerShell" on 10).
  const TERMINAL_HOWTO: Record<Os, string> = {
    mac: 'Press ⌘ + Space, type "Terminal", press Enter.',
    linux: "Press Ctrl + Alt + T (or open Terminal from your applications).",
    windows:
      'Click Start (or press the Windows key), type "PowerShell", and open Windows PowerShell.',
  };

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

      <nav class="tabs" style="margin:2px 0 16px">
        <button class:active={osTab === "mac"} onclick={() => (osTab = "mac")}>macOS</button>
        <button class:active={osTab === "linux"} onclick={() => (osTab = "linux")}>Linux</button>
        <button class:active={osTab === "windows"} onclick={() => (osTab = "windows")}>Windows</button>
      </nav>

      <!-- Prerequisites BEFORE the commands: both of them need Python and Git
           already installed, so finding this underneath meant reading it after
           the command had already failed. -->
      <div class="note">
        <p>
          <b>First, install these</b> —
          <a href="https://www.python.org/downloads/" target="_blank" rel="noopener">Python 3</a>
          and <a href="https://git-scm.com/downloads" target="_blank" rel="noopener">Git</a>.
          {#if isWin}
            When installing Python, tick <b>"Add python.exe to PATH"</b>.
          {/if}
        </p>
        {#if isWin}
          <p>
            If <span class="mono">py</span> isn't recognized afterwards, try
            <span class="mono">python</span>.
          </p>
        {/if}
      </div>

      <ol class="steps">
        <li>
          <h3>Open your terminal</h3>
          <p class="lede">{TERMINAL_HOWTO[osTab]}</p>
        </li>
        {#if isWin}
          <!-- Windows leads with clone + run. The piped one-liner has a
               reported failure on Windows that we could not reproduce or
               root-cause (see the disclosure below), and this path is
               known-good — so it is the instruction, not the fallback. -->
          <li>
            <h3>Paste this and press Enter</h3>
            <p class="lede">Downloads the swarm code into a folder here.</p>
            <CopyCommand text={cloneCmd()} multiline />
          </li>
          <li>
            <h3>Then paste this</h3>
            <p class="lede">Saves your credentials and opens the setup page.</p>
            <CopyCommand text={runJoinCmd()} />
          </li>
        {:else}
          <li>
            <h3>Paste this and press Enter</h3>
            <CopyCommand text={bootstrapCmd()} label="Copy command" />
          </li>
        {/if}
        <!-- The step people used to have to infer. Without it the page ends on
             a command, so someone who checks back here has no idea whether
             they're finished. -->
        <li>
          <h3>Finish in the setup page</h3>
          <p class="lede">
            A setup page opens at <span class="mono">127.0.0.1:8787</span>.
            Choose <b>Join a swarm</b>, follow the steps there — add your API
            key — and launch your fleet.
          </p>
        </li>
      </ol>

      {#if isWin}
        <details class="alt">
          <summary>One-line install (experimental on Windows)</summary>
          <p class="lede" style="margin:10px 0 8px">
            Skips the clone and keeps the code in your app-data folder. This has
            been reported to fail silently on Windows — most likely because
            <span class="mono">python</span> resolves to the Microsoft Store
            alias, which exits without output when run in a pipe. Use the steps
            above if it prints nothing.
          </p>
          <CopyCommand text={bootstrapCmd()} />
        </details>
      {:else}
        <details class="alt">
          <summary>Clone the repo instead</summary>
          <p class="lede" style="margin:10px 0 8px">
            Puts the code in a folder you choose, rather than the managed
            checkout the one-liner keeps under your data directory.
          </p>
          <ol class="steps compact">
            <li><CopyCommand text={cloneCmd()} /></li>
            <li><CopyCommand text={runJoinCmd()} /></li>
          </ol>
        </details>
      {/if}

      <!-- The host's own case: they created the swarm from the companion app,
           which is already running on :8787, and now want to contribute too.
           Previously this page only spoke to people starting from nothing. -->
      <details class="alt">
        <summary>Already have the setup app open?</summary>
        <p class="lede" style="margin:10px 0 8px">
          If <span class="mono">127.0.0.1:8787</span> is already running — you
          host this swarm, or you've joined one before — skip the commands. Open
          it, choose <b>Join a swarm</b>, and paste these into the connect step.
        </p>
        <CopyCommand text={configBlock()} multiline />
        <p class="lede" style="margin-top:10px">
          Hosts can also do this without a join link: on the fleet page after
          provisioning, use <b>Also run agents yourself</b>.
        </p>
      </details>
    </div>
  {/if}
</div>

<style>
  /* `.tabs`, `.alt` and the command-box styling now live in app.css /
     CopyCommand.svelte — three surfaces render them. */
  .steps {
    margin: 10px 0 0;
    padding-left: 22px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .steps h3 {
    font-family: var(--ui);
    font-size: 15px;
    font-weight: 600;
    margin: 0 0 4px;
  }
  /* The numbered step for each command reads as the instruction; inside a
     disclosure the commands are just a sequence, so drop the headings' spacing. */
  .steps.compact { gap: 10px; margin-top: 4px; }
  .steps .lede { margin: 0 0 8px; }

  /* Prerequisites block above the steps. Same treatment as the companion's
     .note — an aside with weight, not fine print, because skipping it is what
     makes the commands below fail. */
  .note {
    margin: 4px 0 18px;
    padding: 14px 16px;
    background: var(--bg-sunken);
    border-radius: 6px;
    font-size: 13.5px;
    line-height: 1.6;
    color: var(--ink-mid);
  }
  .note p { margin: 0; }
  .note p + p { margin-top: 8px; }
  .note b { color: var(--ink); font-weight: 600; }
</style>
