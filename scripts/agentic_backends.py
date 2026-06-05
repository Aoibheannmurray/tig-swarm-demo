"""Agentic (tooled) backends for the swarm loop.

Mode 2 of the claude-code provider: instead of a single-shot completion that
returns a code blob the loop parses, the agent runs in headless mode inside a
sandboxed git worktree with file-edit tools. It edits the algorithm file
directly and writes its hypothesis to .swarm/hypothesis.json before stopping.

The loop still owns server I/O (state, heartbeat, publish) and the official
benchmark. The agent's job is bounded to: edit algorithm files + write
hypothesis.

AgenticBackend is the protocol; ClaudeCodeAgent is the only concrete
implementation today. CodexAgent is stubbed so the dispatch point in
run_loop.py knows the slot exists for a future contributor.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


# ── CLI resolution ─────────────────────────────────────────────────


def _resolve_cli(name: str, env_override: str | None = None) -> str | None:
    """Find a backend CLI binary, with Windows-specific fallbacks.

    Resolution order:
      1. `env_override` env var (e.g. `CODEX_CLI`) — explicit absolute path,
         honored if it exists. Lets users on Windows point at the npm
         install (`%APPDATA%\\npm\\codex.cmd`) when the Windows-Store/App
         alias (`%LOCALAPPDATA%\\Microsoft\\WindowsApps\\<name>.exe`) shadows
         the real CLI and returns "Access is denied".
      2. `shutil.which(name)`, which on Windows already honors PATHEXT and
         finds `.cmd` / `.bat` shims.
      3. On Windows specifically, `shutil.which(name + ".cmd")` as a
         belt-and-braces fallback for unusual PATH layouts.

    Returns the resolved path or None if nothing was found."""
    if env_override:
        candidate = os.environ.get(env_override, "").strip()
        if candidate and Path(candidate).exists():
            return candidate

    found = shutil.which(name)
    if found:
        return found

    if sys.platform == "win32":
        for suffix in (".cmd", ".exe", ".bat"):
            found = shutil.which(name + suffix)
            if found:
                return found
    return None


def _wrap_for_windows(argv: list[str]) -> list[str]:
    """Run `.cmd` / `.bat` scripts via cmd.exe so subprocess.run finds them.

    Python's subprocess on Windows can usually execute `.cmd` directly, but
    only when PATHEXT is set up *for the subprocess's environment* and the
    binary is on PATH. When we pass an absolute path resolved from
    `%APPDATA%\\npm\\codex.cmd` (the npm install for Codex CLI), subprocess
    sometimes refuses to launch it without an explicit `cmd.exe /d /c`
    prefix — surfaced as `[WinError 193] %1 is not a valid Win32
    application`. Wrap proactively to avoid that gotcha."""
    if sys.platform != "win32" or not argv:
        return argv
    first = argv[0]
    if first.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/c"] + argv
    return argv


@dataclass
class AgenticResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool


class AgenticBackend(Protocol):
    name: str
    cli_name: str  # binary the loop precheck should look for (e.g. "claude", "codex")

    def prepare(self, workdir: Path, challenge_md: str, config: dict) -> None: ...

    def iterate(
        self, workdir: Path, user_prompt: str,
        *, model: str | None, timeout_s: int,
    ) -> AgenticResult: ...


# ── Claude Code ────────────────────────────────────────────────────


# Files the agent is allowed to mutate in the worktree. Anything else is
# read-only via Read/Glob/Grep (which the harness scopes to cwd by default).
_HYPOTHESIS_RELPATH = ".swarm/hypothesis.json"
_CLAUDE_MD_RELPATH = "CLAUDE.md"
_SETTINGS_RELPATH = ".swarm/sandbox-settings.json"


def _build_sandbox_settings(config: dict, workdir: Path) -> dict:
    """Permissions for the Claude Code sandbox (see SANDBOX_SPEC.md).

    Read (§1): scoped to the active challenge's own directory + root
    CHALLENGE.md + Cargo.toml. NOT the whole worktree — other challenges,
    scripts/, server/, .git/, datasets/ and the challenge's README/baselines
    are excluded. Read-tool scope is independent of what's on disk: cargo
    still sees the full crate, we only limit what the agent pulls into context.

    Edit (§2): the algorithm file (and CUDA kernels if GPU) + the hypothesis
    file. Write(**) denied — force Edit, no new files.

    Bash (§4): compile/analyze only, never executing agent code — cargo
    check/build/fmt/clippy, plus the kernel PTX compile for GPU challenges.

    Deny (§3/§5): WebFetch/WebSearch, ALL git (including read-only log/show —
    they'd reach the shared object store and bypass the §1 read-scope), any
    network-touching Bash command, and filesystem mutation/privilege.

    Permission-model facts (verified against claude 2.1.x, see SANDBOX_SPEC.md
    "Implementation log"):
      - Path rules use BARE-relative globs (`Read(src/x/**)`). The '/'-anchored
        "project-root" form (`Read(/src/x/**)`) silently matches nothing.
      - Reads are DEFAULT-ALLOW: an unlisted path is readable. So scoping is
        done by explicit denies, NOT by an allowlist — and sibling challenge
        dirs must be enumerated (there is no negation glob, and we cannot deny
        `src/**` because deny > allow would then also block the active dir).
    """
    from posixpath import basename, dirname

    algo_relpath = config["algorithm_path"]
    kernel_relpath = config.get("kernel_path")
    # src/<challenge>/algorithm/mod.rs -> src/<challenge>
    challenge_dir = dirname(dirname(algo_relpath))
    src_dir = dirname(challenge_dir) or "src"        # src
    active = basename(challenge_dir)                 # <challenge>

    allow = [
        f"Edit({algo_relpath})",
        f"Edit({_HYPOTHESIS_RELPATH})",
        "Bash(cargo check:*)",
        "Bash(cargo build:*)",
        "Bash(cargo fmt:*)",
        "Bash(cargo clippy:*)",
    ]
    if kernel_relpath:
        allow.append(f"Edit({kernel_relpath})")
        # §4: compile-check the kernel (cu -> ptx). Runs nvcc via the trusted
        # build script — compiles, never executes agent code.
        allow.append("Bash(python3 scripts/build_ptx.py:*)")

    # §1 read-scope (bare-relative). Reads are default-allow, so these allows
    # are largely documentation of intent; the denies below do the scoping.
    read_scope = [
        f"{challenge_dir}/**",
        "CHALLENGE.md",
        "Cargo.toml",
    ]
    for tool in ("Read", "Glob", "Grep"):
        allow += [f"{tool}({p})" for p in read_scope]

    # Deny anything that could exfiltrate, push to a remote, escalate, or
    # mutate files outside the algorithm scope.
    deny = [
        "Write(**)",
        "WebFetch",
        "WebSearch",
        "Bash(git:*)",   # §5 — ALL git, including read-only (log/show/diff/cat-file)
        "Bash(gh:*)",
        "Bash(curl:*)",
        "Bash(wget:*)",
        "Bash(nc:*)",
        "Bash(ssh:*)",
        "Bash(scp:*)",
        "Bash(rsync:*)",
        "Bash(rm:*)",
        "Bash(sudo:*)",
        "Bash(chmod:*)",
        "Bash(chown:*)",
        "Bash(mv:*)",
        "Bash(cp:*)",
        "Bash(dd:*)",
        "Bash(mkfs:*)",
    ]
    # §1 read exclusions (bare-relative). Everything outside the challenge dir,
    # plus the in-dir carve-outs (README/baselines), plus .git (so the object
    # store can't be read directly — §5) and secrets.
    excluded_reads = [
        ".git/**",
        "scripts/**",
        "server/**",
        "target/**",
        "datasets/**",
        "initial_algorithms/**",
        f"{src_dir}/*.rs",                        # top-level harness (main_*, lib.rs)
        f"{challenge_dir}/README.md",
        f"{challenge_dir}/baselines/**",
        ".env",
        "**/*.env",
    ]
    # Enumerate sibling challenge dirs — reads are default-allow, so each
    # non-active dir under src/ must be denied explicitly.
    src_path = workdir / src_dir
    if src_path.is_dir():
        for child in sorted(src_path.iterdir()):
            if child.is_dir() and child.name != active:
                excluded_reads.append(f"{src_dir}/{child.name}/**")
    for tool in ("Read", "Glob", "Grep"):
        deny += [f"{tool}({p})" for p in excluded_reads]

    return {"permissions": {"allow": allow, "deny": deny}}


def _build_claude_md(challenge_md: str, config: dict) -> str:
    """Stable, per-iteration rules dropped into the worktree's CLAUDE.md.

    Claude Code auto-discovers CLAUDE.md from the cwd and adds it to the
    system prompt — so this is where the "rules of the game" live. The
    per-iteration variable state (current best score, prior hypotheses,
    inspiration code) goes in the user prompt instead.
    """
    challenge = config.get("challenge", "unknown")
    algo_relpath = config["algorithm_path"]
    kernel_relpath = config.get("kernel_path")
    timeout = config.get("timeout", 30)

    from prompts import get_strategy_tags
    strategy_tags = ", ".join(f"`{t}`" for t in get_strategy_tags(config))

    files_section = f"- `{algo_relpath}` — the algorithm file. EDIT this."
    if kernel_relpath:
        files_section += f"\n- `{kernel_relpath}` — CUDA kernels. EDIT this if needed."

    # GPU challenges: the kernel is NOT compiled by cargo (it's compiled to
    # PTX separately), so give the agent the compile-check command for it.
    kernel_bash = (
        f", and `python3 scripts/build_ptx.py {challenge}` to compile-check "
        f"your CUDA kernel (cargo does NOT compile `.cu` files)"
        if kernel_relpath else ""
    )

    # Optimizer-hook challenges (neuralnet_optimizer): the training loop owns
    # save_solution, and the agent gets the full optimizer-hook contract.
    opt_hooks = challenge in {"neuralnet_optimizer"}
    if opt_hooks:
        from prompts import OPTIMIZER_HOOK_CONTRACT as opt_contract
        time_bullet = (
            f"- Per-instance time budget: {timeout} seconds — the harness-owned training "
            f"loop is killed at this hard deadline and its best checkpoint is scored. Keep "
            f"your optimizer hooks fast so more epochs fit; the harness calls save_solution "
            f"for you (do NOT call it yourself, and do NOT write your own loop)."
        )
    else:
        opt_contract = ""
        time_bullet = (
            f"- Per-instance time budget: {timeout} seconds. The solver is killed at this\n"
            f"  hard deadline. Use a time-based loop (`std::time::Instant`), call\n"
            f"  `save_solution()` early with your first feasible solution, then keep\n"
            f"  improving and re-saving. The last saved solution is what gets scored."
        )

    return f"""\
# Swarm contributor — agentic mode

You are one autonomous contributor in a swarm trying to improve a Rust solver
for the **{challenge}** TIG challenge. The driver loop (Python) handles all
communication with the coordination server — your job is bounded.

## Your job each iteration

1. Read the user prompt for the current state: your best score, prior
   hypotheses you've already tried, inspiration code (if any), and any
   stagnation hints.
2. Decide on ONE specific improvement to try.
3. Edit ONLY the algorithm file(s) listed below to implement it.
4. Validate it compiles with `cargo check --features solver,{challenge}`.
5. Before stopping, write your hypothesis as JSON to `.swarm/hypothesis.json`
   (schema below). This is how the driver loop knows what you tried.

## Files you may edit

{files_section}
- `.swarm/hypothesis.json` — write your hypothesis here before stopping.

You may **read** only what you need to write the algorithm: this challenge's
own directory, the root `CHALLENGE.md`, and `Cargo.toml`. Other challenges,
`scripts/`, `server/`, git history, and this challenge's `README.md` /
`baselines/` are out of scope — the sandbox rejects reads of them. You may
NOT edit anything outside the list above either; only the algorithm file (and
kernel) are scored, so keep your change self-contained in those files.

## Hypothesis file schema

Write `.swarm/hypothesis.json` with exactly this shape:

```json
{{
  "title": "short title under 80 chars",
  "description": "2-3 sentences describing what you changed and why",
  "strategy_tag": "one of the strategy tags below",
  "notes": "brief implementation notes"
}}
```

Strategy tags (pick the closest match): {strategy_tags}.

## Tools you have

- `Read`, `Glob`, `Grep` — explore this challenge's directory + `CHALLENGE.md`
  + `Cargo.toml` (other paths are blocked).
- `Edit` — modify allowed files.
- `Bash` — only `cargo check`, `cargo build`, `cargo fmt`, `cargo clippy`{kernel_bash}.

You do NOT have network access, you cannot run `git`, `curl`, `wget`, `rm`,
or any shell command outside the allowlist. You do NOT publish results
yourself — the driver loop runs the official benchmark after you exit and
publishes the score paired with your hypothesis.

## Solver constraints

- `use super::*;` must remain the first import in the Rust file.
- Keep the harness entry points and their signatures unchanged: for most
  challenges that is `fn solve_challenge(`; for `neuralnet_optimizer` it is the
  `pub fn optimizer_init_state` / `optimizer_query_at_params` / `optimizer_step`
  hooks (the training loop and `solve_challenge` are harness-owned — do not add
  or rename them). The harness calls these by name.
{time_bullet}
- Do not remove `unsafe` blocks that are already there; do not add new
  `unsafe` unless you understand the invariants.

## When to stop

Stop as soon as your edit compiles AND you have written
`.swarm/hypothesis.json`. The driver will then run the official benchmark.
Don't run `scripts/benchmark.py` yourself — that's the driver's job and
self-running it wastes time.

## Challenge-specific details

{challenge_md}
{opt_contract}
"""


class ClaudeCodeAgent:
    """Headless Claude Code with file-edit tools, sandboxed to a worktree."""

    name = "claude-code-agentic"
    cli_name = "claude"
    cli_env_override = "CLAUDE_CLI"

    def resolve_cli(self) -> str | None:
        return _resolve_cli(self.cli_name, self.cli_env_override)

    def prepare(self, workdir: Path, challenge_md: str, config: dict) -> None:
        """Write CLAUDE.md + sandbox-settings.json into the worktree.

        Idempotent — safe to call every iteration. CLAUDE.md is small and
        the challenge may have switched between iterations, so we rewrite
        rather than try to cache.
        """
        swarm_dir = workdir / ".swarm"
        swarm_dir.mkdir(exist_ok=True)

        settings = _build_sandbox_settings(config, workdir)
        (workdir / _SETTINGS_RELPATH).write_text(
            json.dumps(settings, indent=2) + "\n"
        )
        (workdir / _CLAUDE_MD_RELPATH).write_text(
            _build_claude_md(challenge_md, config)
        )

    def iterate(
        self, workdir: Path, user_prompt: str,
        *, model: str | None, timeout_s: int,
    ) -> AgenticResult:
        """Run `claude -p` with tooled access inside the worktree.

        Sends the per-iteration user prompt via stdin. CLAUDE.md
        auto-discovery picks up the rules we wrote in prepare(). Settings
        file applies the permission sandbox. stdout/stderr captured and
        returned for logging + fallback hypothesis synthesis.
        """
        claude_bin = self.resolve_cli()
        if claude_bin is None:
            raise RuntimeError(
                "claude CLI not found on PATH. Install Claude Code "
                "(https://docs.claude.com/en/docs/claude-code) or switch to "
                "--provider claude-code (one-shot mode) or an API provider. "
                "On Windows you can also export CLAUDE_CLI to point at the "
                "absolute path of your `claude` install."
            )

        cmd = _wrap_for_windows([
            claude_bin, "-p",
            "--settings", str(workdir / _SETTINGS_RELPATH),
            "--permission-mode", "acceptEdits",
            "--add-dir", str(workdir),
        ])
        if model:
            cmd += ["--model", model]

        t0 = time.time()
        try:
            result = subprocess.run(
                cmd, input=user_prompt,
                capture_output=True, text=True,
                cwd=workdir, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            return AgenticResult(
                stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
                stderr=(e.stderr or "") if isinstance(e.stderr, str) else "",
                exit_code=-1,
                duration_s=time.time() - t0,
                timed_out=True,
            )
        return AgenticResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            duration_s=time.time() - t0,
            timed_out=False,
        )


# ── Codex ──────────────────────────────────────────────────────────


_AGENTS_MD_RELPATH = "AGENTS.md"
_LAST_MESSAGE_RELPATH = ".swarm/last_message.txt"


def _build_agents_md(challenge_md: str, config: dict) -> str:
    """Codex's analog of CLAUDE.md — auto-discovered from cwd.

    Codex's sandbox is coarser than Claude's (mode-level instead of
    per-tool patterns), so the file-scope rules here are *soft*
    instructions: the agent has workspace-write access to the whole
    worktree but is told to only edit the algorithm files. Edits to
    anything else get silently dropped (the loop only copies the
    algorithm file back into the main checkout), so violations cause the
    iteration's hypothesis to under-deliver rather than escape the
    sandbox.
    """
    challenge = config.get("challenge", "unknown")
    algo_relpath = config["algorithm_path"]
    kernel_relpath = config.get("kernel_path")
    timeout = config.get("timeout", 30)

    from prompts import get_strategy_tags
    strategy_tags = ", ".join(f"`{t}`" for t in get_strategy_tags(config))

    files_section = f"- `{algo_relpath}` — the algorithm file. EDIT this."
    if kernel_relpath:
        files_section += f"\n- `{kernel_relpath}` — CUDA kernels. EDIT this if needed."

    opt_hooks = challenge in {"neuralnet_optimizer"}
    if opt_hooks:
        from prompts import OPTIMIZER_HOOK_CONTRACT as opt_contract
        time_bullet = (
            f"- Per-instance time budget: {timeout} seconds — the harness-owned training "
            f"loop is killed at this hard deadline and its best checkpoint is scored. Keep "
            f"your optimizer hooks fast so more epochs fit; the harness calls save_solution "
            f"for you (do NOT call it yourself, and do NOT write your own loop)."
        )
    else:
        opt_contract = ""
        time_bullet = (
            f"- Per-instance time budget: {timeout} seconds. The solver is killed at this\n"
            f"  hard deadline. Use a time-based loop (`std::time::Instant`), call\n"
            f"  `save_solution()` early with your first feasible solution, then keep\n"
            f"  improving and re-saving. The last saved solution is what gets scored."
        )

    return f"""\
# Swarm contributor — Codex agent mode

You are one autonomous contributor in a swarm trying to improve a Rust solver
for the **{challenge}** TIG challenge. The driver loop (Python) handles all
communication with the coordination server — your job is bounded.

## Your job each iteration

1. Read the user prompt for the current state: your best score, prior
   hypotheses you've already tried, inspiration code (if any), and any
   stagnation hints.
2. Decide on ONE specific improvement to try.
3. Edit ONLY the algorithm file(s) listed below to implement it.
4. Validate it compiles with `cargo check --features solver,{challenge}`.
5. Before stopping, write your hypothesis as JSON to `.swarm/hypothesis.json`
   (schema below). This is how the driver loop knows what you tried.

## Files you may edit

{files_section}
- `.swarm/hypothesis.json` — write your hypothesis here before stopping.

The sandbox is `workspace-write` — you technically have write access to
the whole worktree. **Do not use it.** The driver only copies the
algorithm file(s) back to the main checkout when scoring — any other
edits you make get silently discarded, so editing Cargo.toml, src/lib.rs,
or any other file is a waste of your turns and will cause your
hypothesis to underperform.

## Hypothesis file schema

Write `.swarm/hypothesis.json` with exactly this shape:

```json
{{
  "title": "short title under 80 chars",
  "description": "2-3 sentences describing what you changed and why",
  "strategy_tag": "one of the strategy tags below",
  "notes": "brief implementation notes"
}}
```

Strategy tags (pick the closest match): {strategy_tags}.

## Sandbox

- Sandbox mode: `workspace-write` (rooted at this worktree).
- Network access is DISABLED — no `curl`, `wget`, package downloads, or
  outbound HTTP. `cargo check` works because dependencies are already
  vendored/cached.
- Approval policy is `never` — there's nobody to approve prompts. If you
  hit a permission wall, work around it within these rules.

## Solver constraints

- `use super::*;` must remain the first import in the Rust file.
- Keep the harness entry points and their signatures unchanged: for most
  challenges that is `fn solve_challenge(`; for `neuralnet_optimizer` it is the
  `pub fn optimizer_init_state` / `optimizer_query_at_params` / `optimizer_step`
  hooks (the training loop and `solve_challenge` are harness-owned — do not add
  or rename them). The harness calls these by name.
{time_bullet}
- Do not remove `unsafe` blocks that are already there; do not add new
  `unsafe` unless you understand the invariants.

## When to stop

Stop as soon as your edit compiles AND you have written
`.swarm/hypothesis.json`. The driver runs the official benchmark after
you stop — don't run `scripts/benchmark.py` yourself, that wastes time.

## Challenge-specific details

{challenge_md}
{opt_contract}
"""


class CodexAgent:
    """Headless OpenAI Codex (`codex exec`), sandboxed to a worktree."""

    name = "codex-agentic"
    cli_name = "codex"
    cli_env_override = "CODEX_CLI"

    def resolve_cli(self) -> str | None:
        return _resolve_cli(self.cli_name, self.cli_env_override)

    def prepare(self, workdir: Path, challenge_md: str, config: dict) -> None:
        """Write AGENTS.md into the worktree. Codex auto-discovers it."""
        (workdir / ".swarm").mkdir(exist_ok=True)
        (workdir / _AGENTS_MD_RELPATH).write_text(
            _build_agents_md(challenge_md, config)
        )

    def iterate(
        self, workdir: Path, user_prompt: str,
        *, model: str | None, timeout_s: int,
    ) -> AgenticResult:
        """Shell `codex exec` with workspace-write sandbox in the worktree.

        The prompt arrives on stdin. `--output-last-message <FILE>` writes
        the agent's final text message to disk so we can use it for
        fallback hypothesis synthesis instead of fishing it out of the
        JSON-ish stdout trace. Approval policy is forced to "never" since
        we're non-interactive; network access is forced off so the agent
        can't curl-exfiltrate or pull new crates mid-iteration.
        """
        codex_bin = self.resolve_cli()
        if codex_bin is None:
            raise RuntimeError(
                "codex CLI not found on PATH. Install Codex CLI "
                "(`npm install -g @openai/codex` or "
                "https://github.com/openai/codex) or switch to "
                "--provider claude-code-agentic / an API provider. "
                "On Windows, export CODEX_CLI to the npm install path "
                "(e.g. `%APPDATA%\\npm\\codex.cmd`) if the Windows Store "
                "alias is shadowing the real CLI with \"Access is denied\"."
            )

        last_msg_path = workdir / _LAST_MESSAGE_RELPATH
        if last_msg_path.exists():
            last_msg_path.unlink()

        cmd = _wrap_for_windows([
            codex_bin, "exec",
            "--sandbox", "workspace-write",
            "-C", str(workdir),
            "--output-last-message", str(last_msg_path),
            "--skip-git-repo-check",
            "-c", 'approval_policy="never"',
            "-c", "sandbox_workspace_write.network_access=false",
        ])
        if model:
            cmd += ["--model", model]

        t0 = time.time()
        try:
            result = subprocess.run(
                cmd, input=user_prompt,
                capture_output=True, text=True,
                cwd=workdir, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
            stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
            # Even on timeout, the agent may have written the last-message
            # file before the deadline — surface it for fallback hypothesis.
            if last_msg_path.exists():
                try:
                    stdout = last_msg_path.read_text() or stdout
                except OSError:
                    pass
            return AgenticResult(
                stdout=stdout, stderr=stderr, exit_code=-1,
                duration_s=time.time() - t0, timed_out=True,
            )

        # Prefer the agent's final message over the full event trace. The
        # trace is verbose tool-call JSONL; the last_message is the
        # human-readable summary the agent wrote on its last turn.
        stdout = result.stdout
        if last_msg_path.exists():
            try:
                final = last_msg_path.read_text()
                if final:
                    stdout = final
            except OSError:
                pass

        return AgenticResult(
            stdout=stdout, stderr=result.stderr,
            exit_code=result.returncode,
            duration_s=time.time() - t0, timed_out=False,
        )


# ── Dispatch ───────────────────────────────────────────────────────


def get_backend(provider: str) -> AgenticBackend:
    if provider == "claude-code-agentic":
        return ClaudeCodeAgent()
    if provider == "codex-agentic":
        return CodexAgent()
    raise ValueError(f"Unknown agentic provider: {provider}")
