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
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from proc_utils import run_tree


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


# Secrets the coding-agent subprocess must not inherit. Both backends allow
# Bash(cargo …), and build.rs runs agent-controlled code — anything left in
# the environment is exfiltratable. Neither CLI needs any of these: claude
# uses its own stored login, codex its own `codex login` credentials.
# Everything else (PATH, HOME, CLAUDE_CLI/CODEX_CLI, proxy vars, …) passes
# through untouched.
_SENSITIVE_ENV_KEYS = frozenset({"ADMIN_KEY", "SWARM_PASSWORD", "C3_API_KEY"})
_SENSITIVE_ENV_PREFIXES = (
    "OPENAI_", "ANTHROPIC_", "GOOGLE_", "OPENROUTER_", "VENICE_",
)


def _scrubbed_env() -> dict[str, str]:
    """A copy of os.environ with provider API keys and swarm secrets removed."""
    return {
        k: v for k, v in os.environ.items()
        if k not in _SENSITIVE_ENV_KEYS
        and not k.endswith("_API_KEY")
        and not k.startswith(_SENSITIVE_ENV_PREFIXES)
    }


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

    def prepare(
        self, workdir: Path, challenge_md: str, config: dict,
        *, extraction: bool = False,
    ) -> None: ...

    def iterate(
        self, workdir: Path, user_prompt: str,
        *, model: str | None, timeout_s: int,
    ) -> AgenticResult: ...


# ── Claude Code ────────────────────────────────────────────────────


# Files the agent is allowed to mutate in the worktree. Anything else is
# read-only via Read/Glob/Grep (which the harness scopes to cwd by default).
_HYPOTHESIS_RELPATH = ".swarm/hypothesis.json"
_HYPERPARAMS_RELPATH = ".swarm/hyperparameters.json"
_CLAUDE_MD_RELPATH = "CLAUDE.md"
_SETTINGS_RELPATH = ".swarm/sandbox-settings.json"


def _build_sandbox_settings(config: dict, workdir: Path, *, extraction: bool = False) -> dict:
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
    # src/<challenge>/algorithm/mod.rs -> src/<challenge>/algorithm
    algo_dir = dirname(algo_relpath)
    # src/<challenge>/algorithm/mod.rs -> src/<challenge>
    challenge_dir = dirname(dirname(algo_relpath))
    src_dir = dirname(challenge_dir) or "src"        # src
    active = basename(challenge_dir)                 # <challenge>

    allow = [
        f"Edit({algo_relpath})",
        # Multi-file algorithms keep sidecar sources (helpers.rs, config.rs, …)
        # in the algorithm dir; let the agent edit any of them. Write(**) stays
        # denied below, so no NEW files can be created — edits only.
        f"Edit({algo_dir}/**)",
        f"Edit({_HYPOTHESIS_RELPATH})",
        "Bash(cargo check:*)",
        "Bash(cargo build:*)",
        "Bash(cargo fmt:*)",
        "Bash(cargo clippy:*)",
    ]
    # Hyperparameter-extraction pass (Fix 1): also let the agent write the spec
    # file. Edit (not Write) keeps it consistent with how the hypothesis file is
    # handled — Write(**) stays denied, so no other new files can be created.
    if extraction:
        allow.append(f"Edit({_HYPERPARAMS_RELPATH})")
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


# ── Shared worktree-doc prose ──────────────────────────────────────
# CLAUDE.md (_build_claude_md) and AGENTS.md (_build_agents_md) are the same
# document modulo the backend-specific sandbox/tooling sections. The shared
# sections live here once; each builder composes them around its own
# backend-specific chunks. Keep the rendered output stable — the wording is
# part of the agents' operating contract.

_DOC_INTRO_TEMPLATE = """\
# {title}

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
"""

_HYPOTHESIS_SCHEMA_TEMPLATE = """\
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
"""

_SOLVER_CONSTRAINTS_TEMPLATE = """\
## Solver constraints

- The existing `use` imports at the top of the starting file must remain
  (e.g. `use tig_challenges::<challenge>::*;`).
{entry_points_bullet}
{time_bullet}
- Do not remove `unsafe` blocks that are already there; do not add new
  `unsafe` unless you understand the invariants.
"""

_CHALLENGE_DETAILS_TEMPLATE = """\
## Challenge-specific details

{challenge_md}
{opt_contract}
"""


def _editable_files_section(config: dict) -> str:
    """The bullet list of algorithm files the agent may edit."""
    files_section = f"- `{config['algorithm_path']}` — the algorithm file. EDIT this."
    kernel_relpath = config.get("kernel_path")
    if kernel_relpath:
        files_section += f"\n- `{kernel_relpath}` — CUDA kernels. EDIT this if needed."
    return files_section


def _strategy_tags_line(config: dict) -> str:
    from prompts import get_strategy_tags
    return ", ".join(f"`{t}`" for t in get_strategy_tags(config))


def _entry_points_bullet(challenge: str) -> str:
    """The harness-entry-point constraint, specialized per challenge.

    Only the entry point(s) the active challenge actually has are named — a
    knapsack agent shouldn't be told about neuralnet optimizer hooks it will
    never touch.
    """
    if challenge in {"neuralnet_optimizer"}:
        return (
            "- Keep the harness entry points and their signatures unchanged: the\n"
            "  `pub fn optimizer_init_state` / `optimizer_query_at_params` /\n"
            "  `optimizer_step` hooks. The training loop and `solve_challenge` are\n"
            "  harness-owned — do not add or rename them. The harness calls these\n"
            "  hooks by name."
        )
    return (
        "- Keep the harness entry point and its signature unchanged: `fn\n"
        "  solve_challenge(`. Do not rename it or add a competing entry point —\n"
        "  the harness calls it by name."
    )


def _time_budget_parts(challenge: str) -> tuple[str, str]:
    """(time_bullet, opt_contract) for the solver-constraints section.

    Optimizer-hook challenges (neuralnet_optimizer): the training loop owns
    save_solution, and the agent gets the full optimizer-hook contract.
    """
    if challenge in {"neuralnet_optimizer"}:
        from prompts import OPTIMIZER_HOOK_CONTRACT as opt_contract
        time_bullet = (
            "- Bounding is by FUEL, not wall-clock: the harness-owned training loop runs "
            "until the challenge's fuel budget is exhausted and its best checkpoint is "
            "scored. Keep your optimizer hooks lean (fewer instructions => more epochs fit "
            "the fuel budget); the harness calls save_solution for you (do NOT call it "
            "yourself, and do NOT write your own loop). Avoid clock-based control flow."
        )
        return time_bullet, opt_contract
    time_bullet = (
        "- Bounding is by FUEL, not wall-clock: your solver runs until it exhausts the\n"
        "  challenge's fuel budget (instruction-counted, deterministic) or returns. Call\n"
        "  `save_solution()` early with your first feasible solution, then keep improving\n"
        "  and re-saving — the last saved solution is scored, and the runtime stops you\n"
        "  at the fuel cap. Do NOT gate the loop on `std::time::Instant` / a wall-clock\n"
        "  deadline: clock-based control flow makes fuel usage nondeterministic."
    )
    return time_bullet, ""


def _build_claude_md(challenge_md: str, config: dict) -> str:
    """Stable, per-iteration rules dropped into the worktree's CLAUDE.md.

    Claude Code auto-discovers CLAUDE.md from the cwd and adds it to the
    system prompt — so this is where the "rules of the game" live. The
    per-iteration variable state (current best score, prior hypotheses,
    inspiration code) goes in the user prompt instead.
    """
    challenge = config.get("challenge", "unknown")
    kernel_relpath = config.get("kernel_path")
    entry_points_bullet = _entry_points_bullet(challenge)
    time_bullet, opt_contract = _time_budget_parts(challenge)

    # GPU challenges: the kernel is NOT compiled by cargo (it's compiled to
    # PTX separately), so give the agent the compile-check command for it.
    kernel_bash = (
        f", and `python3 scripts/build_ptx.py {challenge}` to compile-check "
        f"your CUDA kernel (cargo does NOT compile `.cu` files)"
        if kernel_relpath else ""
    )

    return (
        _DOC_INTRO_TEMPLATE.format(
            title="Swarm contributor — agentic mode",
            challenge=challenge,
            files_section=_editable_files_section(config),
        )
        + """
You may **read** only what you need to write the algorithm: this challenge's
own directory, the root `CHALLENGE.md`, and `Cargo.toml`. Other challenges,
`scripts/`, `server/`, git history, and this challenge's `README.md` /
`baselines/` are out of scope — the sandbox rejects reads of them. You may
NOT edit anything outside the list above either; only the algorithm file (and
kernel) are scored, so keep your change self-contained in those files.

"""
        + _HYPOTHESIS_SCHEMA_TEMPLATE.format(
            strategy_tags=_strategy_tags_line(config),
        )
        + f"""
## Tools you have

- `Read`, `Glob`, `Grep` — explore this challenge's directory + `CHALLENGE.md`
  + `Cargo.toml` (other paths are blocked).
- `Edit` — modify allowed files.
- `Bash` — only `cargo check`, `cargo build`, `cargo fmt`, `cargo clippy`{kernel_bash}.

You do NOT have network access, you cannot run `git`, `curl`, `wget`, `rm`,
or any shell command outside the allowlist. You do NOT publish results
yourself — the driver loop runs the official benchmark after you exit and
publishes the score paired with your hypothesis.

"""
        + _SOLVER_CONSTRAINTS_TEMPLATE.format(
            entry_points_bullet=entry_points_bullet, time_bullet=time_bullet,
        )
        + """
## When to stop

Stop as soon as your edit compiles AND you have written
`.swarm/hypothesis.json`. The driver will then run the official benchmark.
Don't run `scripts/benchmark.py` yourself — that's the driver's job and
self-running it wastes time.

"""
        + _CHALLENGE_DETAILS_TEMPLATE.format(
            challenge_md=challenge_md, opt_contract=opt_contract,
        )
    )


class ClaudeCodeAgent:
    """Headless Claude Code with file-edit tools, sandboxed to a worktree."""

    name = "claude-code-agentic"
    cli_name = "claude"
    cli_env_override = "CLAUDE_CLI"

    def resolve_cli(self) -> str | None:
        return _resolve_cli(self.cli_name, self.cli_env_override)

    def prepare(
        self, workdir: Path, challenge_md: str, config: dict,
        *, extraction: bool = False,
    ) -> None:
        """Write CLAUDE.md + sandbox-settings.json into the worktree.

        Idempotent — safe to call every iteration. CLAUDE.md is small and
        the challenge may have switched between iterations, so we rewrite
        rather than try to cache. `extraction=True` widens the sandbox to let
        the agent write the hyperparameter spec file (Fix 1).
        """
        swarm_dir = workdir / ".swarm"
        swarm_dir.mkdir(exist_ok=True)

        settings = _build_sandbox_settings(config, workdir, extraction=extraction)
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
                "--provider claude-code (single-shot mode) or an API provider. "
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
        # run_tree: the CLI gets its own process group (POSIX) so a timeout
        # kills its cargo/nvcc/tool grandchildren too, not just the CLI.
        stdout, stderr, exit_code, timed_out = run_tree(
            cmd, input_text=user_prompt,
            cwd=workdir, timeout_s=timeout_s,
            env=_scrubbed_env(),
        )
        return AgenticResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=-1 if timed_out else (exit_code if exit_code is not None else -1),
            duration_s=time.time() - t0,
            timed_out=timed_out,
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
    entry_points_bullet = _entry_points_bullet(challenge)
    time_bullet, opt_contract = _time_budget_parts(challenge)

    return (
        _DOC_INTRO_TEMPLATE.format(
            title="Swarm contributor — Codex agentic mode",
            challenge=challenge,
            files_section=_editable_files_section(config),
        )
        + """
The sandbox is `workspace-write` — you technically have write access to
the whole worktree. **Do not use it.** The driver only copies the
algorithm file(s) back to the main checkout when scoring — any other
edits you make get silently discarded, so editing Cargo.toml, src/lib.rs,
or any other file is a waste of your turns and will cause your
hypothesis to underperform.

"""
        + _HYPOTHESIS_SCHEMA_TEMPLATE.format(
            strategy_tags=_strategy_tags_line(config),
        )
        + """
## Sandbox

- Sandbox mode: `workspace-write` (rooted at this worktree).
- Network access is DISABLED — no `curl`, `wget`, package downloads, or
  outbound HTTP. `cargo check` works because dependencies are already
  vendored/cached.
- Approval policy is `never` — there's nobody to approve prompts. If you
  hit a permission wall, work around it within these rules.

"""
        + _SOLVER_CONSTRAINTS_TEMPLATE.format(
            entry_points_bullet=entry_points_bullet, time_bullet=time_bullet,
        )
        + """
## When to stop

Stop as soon as your edit compiles AND you have written
`.swarm/hypothesis.json`. The driver runs the official benchmark after
you stop — don't run `scripts/benchmark.py` yourself, that wastes time.

"""
        + _CHALLENGE_DETAILS_TEMPLATE.format(
            challenge_md=challenge_md, opt_contract=opt_contract,
        )
    )


class CodexAgent:
    """Headless OpenAI Codex (`codex exec`), sandboxed to a worktree."""

    name = "codex-agentic"
    cli_name = "codex"
    cli_env_override = "CODEX_CLI"

    def resolve_cli(self) -> str | None:
        return _resolve_cli(self.cli_name, self.cli_env_override)

    def prepare(
        self, workdir: Path, challenge_md: str, config: dict,
        *, extraction: bool = False,
    ) -> None:
        """Write AGENTS.md into the worktree. Codex auto-discovers it.

        `extraction` is accepted for interface parity; Codex runs under a
        workspace-write sandbox (no per-file allowlist), so it can already write
        the hyperparameter spec file without widening.
        """
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
        # run_tree: the CLI gets its own process group (POSIX) so a timeout
        # kills its cargo/tool grandchildren too, not just the CLI.
        stdout, stderr, exit_code, timed_out = run_tree(
            cmd, input_text=user_prompt,
            cwd=workdir, timeout_s=timeout_s,
            env=_scrubbed_env(),
        )

        # Prefer the agent's final message over the full event trace. The
        # trace is verbose tool-call JSONL; the last_message is the
        # human-readable summary the agent wrote on its last turn. Even on
        # timeout the agent may have written it before the deadline —
        # surface it for fallback hypothesis synthesis.
        if last_msg_path.exists():
            try:
                final = last_msg_path.read_text()
                if final:
                    stdout = final
            except OSError:
                pass

        return AgenticResult(
            stdout=stdout, stderr=stderr,
            exit_code=-1 if timed_out else (exit_code if exit_code is not None else -1),
            duration_s=time.time() - t0, timed_out=timed_out,
        )


# ── Dispatch ───────────────────────────────────────────────────────


def get_backend(provider: str) -> AgenticBackend:
    if provider == "claude-code-agentic":
        return ClaudeCodeAgent()
    if provider == "codex-agentic":
        return CodexAgent()
    raise ValueError(f"Unknown agentic provider: {provider}")
