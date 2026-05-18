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
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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


def _build_sandbox_settings(config: dict) -> dict:
    """Permissions for the Claude Code sandbox.

    Allow: Edit + Write on any `.rs` / `.cu` file under the algorithm
    directory (so the agent can edit existing siblings, add new modules,
    and add new CUDA kernel files), plus the hypothesis file. Read
    globally inside the worktree. Bash limited to `cargo check` /
    `cargo build` so the agent can self-validate compilation but can't
    shell out to the network or stomp the filesystem. A narrow
    `rm`/`mv` allowance under the algorithm directory lets the agent
    delete or rename sibling modules during a refactor.

    Deny: Write outside the algorithm directory, WebFetch/WebSearch,
    any Bash command that touches the network, git remote state, or
    filesystem deletion/permissions outside the algorithm scope.
    """
    algo_relpath = config["algorithm_path"]
    kernel_relpath = config.get("kernel_path")
    # Algorithm directory, repo-relative. We scope Edit/Write/rm/mv
    # globs to this prefix so the agent has full add/remove freedom
    # inside it but can't touch anything outside.
    algo_dir_rel = str(Path(algo_relpath).parent).replace("\\", "/").rstrip("/") or "."

    allow = [
        # Multi-file edit + write + create under the algorithm dir.
        # Edit() patterns cover edits to existing files; Write() covers
        # new files the agent adds (e.g. a fresh `helpers/spatial.rs`).
        f"Edit({algo_dir_rel}/**/*.rs)",
        f"Write({algo_dir_rel}/**/*.rs)",
        f"Edit({algo_dir_rel}/**/*.cu)",
        f"Write({algo_dir_rel}/**/*.cu)",
        f"Edit({_HYPOTHESIS_RELPATH})",
        f"Write({_HYPOTHESIS_RELPATH})",
        "Read(**)",
        "Glob(**)",
        "Grep(**)",
        "Bash(cargo check:*)",
        "Bash(cargo build:*)",
        "Bash(cargo fmt:*)",
        "Bash(cargo clippy:*)",
        # Narrow rm/mv allowance lets the agent delete or rename sibling
        # modules under the algorithm dir (e.g. during a refactor that
        # consolidates helpers). Anything else is still denied below.
        f"Bash(rm:{algo_dir_rel}/*)",
        f"Bash(rm:{algo_dir_rel}/**)",
        f"Bash(mv:{algo_dir_rel}/*)",
        f"Bash(mv:{algo_dir_rel}/**)",
    ]
    if kernel_relpath:
        # Repo-rooted kernel path (e.g. cuda/kernels.cu) when the kernel
        # lives outside the algorithm directory. Single-file edit
        # allowance, no Write — there's only one canonical location.
        allow.append(f"Edit({kernel_relpath})")

    # Deny anything that could exfiltrate, push to a remote, escalate, or
    # mutate files outside the algorithm scope. Bash needs an explicit deny
    # list because allow-only-matching-prefixes still lets unrelated
    # commands through if the harness defaults to permissive Bash.
    deny = [
        "WebFetch",
        "WebSearch",
        "Bash(curl:*)",
        "Bash(wget:*)",
        "Bash(nc:*)",
        "Bash(ssh:*)",
        "Bash(scp:*)",
        "Bash(rsync:*)",
        "Bash(git push:*)",
        "Bash(git pull:*)",
        "Bash(git fetch:*)",
        "Bash(git clone:*)",
        "Bash(git remote:*)",
        "Bash(git commit:*)",
        "Bash(git reset:*)",
        "Bash(git checkout:*)",
        "Bash(git branch:*)",
        "Bash(git tag:*)",
        "Bash(git rebase:*)",
        "Bash(git merge:*)",
        "Bash(git stash:*)",
        "Bash(git add:*)",
        "Bash(gh:*)",
        "Bash(sudo:*)",
        "Bash(chmod:*)",
        "Bash(chown:*)",
        "Bash(cp:*)",
        "Bash(dd:*)",
        "Bash(mkfs:*)",
    ]
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
    algo_dir_rel = str(Path(algo_relpath).parent).replace("\\", "/").rstrip("/") or "."
    kernel_relpath = config.get("kernel_path")
    timeout = config.get("timeout", 30)

    files_section = (
        f"- Any `.rs` file under `{algo_dir_rel}/` — the algorithm directory.\n"
        f"  - `{algo_dir_rel}/mod.rs` MUST remain and MUST contain "
        f"`fn solve_challenge(`.\n"
        f"  - You may EDIT existing siblings, ADD new ones (wire via "
        f"`mod foo;` in mod.rs), or REMOVE them.\n"
        f"- Any `.cu` file under `{algo_dir_rel}/` — CUDA kernels for GPU "
        f"challenges.\n"
    )
    if kernel_relpath and Path(kernel_relpath).parent.as_posix() != algo_dir_rel:
        # GPU challenges where the kernel file lives outside the algorithm
        # dir (legacy layout) — surface it separately.
        files_section += f"- `{kernel_relpath}` — CUDA kernels. EDIT this if needed.\n"

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

{files_section}- `.swarm/hypothesis.json` — write your hypothesis here before stopping.

You may **read** anything in the worktree (the challenge module, sibling
source files, README, ARCHITECTURE.md). You may NOT edit anything outside
`{algo_dir_rel}/` — the sandbox will reject attempts.

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

Strategy tags (pick the closest match): `construction`, `local_search`,
`metaheuristic`, `constraint_relaxation`, `decomposition`, `hybrid`,
`data_structure`, `greedy`, `dp`, `branch_and_bound`, `other`.

## Tools you have

- `Read`, `Glob`, `Grep` — explore the codebase.
- `Edit` — modify allowed files.
- `Bash` — only `cargo check`, `cargo build`, `cargo fmt`, `cargo clippy`.

You do NOT have network access, you cannot run `git`, `curl`, `wget`, `rm`,
or any shell command outside the cargo allowlist. You do NOT publish results
yourself — the driver loop runs the official benchmark after you exit and
publishes the score paired with your hypothesis.

## Solver constraints

- `use super::*;` must remain the first import in the Rust file.
- `fn solve_challenge(` must remain — do not rename the entry point.
- Per-instance time budget: {timeout} seconds. The solver is killed at this
  hard deadline. Use a time-based loop (`std::time::Instant`), call
  `save_solution()` early with your first feasible solution, then keep
  improving and re-saving. The last saved solution is what gets scored.
- Do not remove `unsafe` blocks that are already there; do not add new
  `unsafe` unless you understand the invariants.

## When to stop

Stop as soon as your edit compiles AND you have written
`.swarm/hypothesis.json`. The driver will then run the official benchmark.
Don't run `scripts/benchmark.py` yourself — that's the driver's job and
self-running it wastes time.

## Challenge-specific details

{challenge_md}
"""


class ClaudeCodeAgent:
    """Headless Claude Code with file-edit tools, sandboxed to a worktree."""

    name = "claude-code-agentic"
    cli_name = "claude"

    def prepare(self, workdir: Path, challenge_md: str, config: dict) -> None:
        """Write CLAUDE.md + sandbox-settings.json into the worktree.

        Idempotent — safe to call every iteration. CLAUDE.md is small and
        the challenge may have switched between iterations, so we rewrite
        rather than try to cache.
        """
        swarm_dir = workdir / ".swarm"
        swarm_dir.mkdir(exist_ok=True)

        settings = _build_sandbox_settings(config)
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
        if shutil.which("claude") is None:
            raise RuntimeError(
                "claude CLI not found on PATH. Install Claude Code "
                "(https://docs.claude.com/en/docs/claude-code) or switch to "
                "--provider claude-code (one-shot mode) or an API provider."
            )

        cmd = [
            "claude", "-p",
            "--settings", str(workdir / _SETTINGS_RELPATH),
            "--permission-mode", "acceptEdits",
            "--add-dir", str(workdir),
        ]
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
    algo_dir_rel = str(Path(algo_relpath).parent).replace("\\", "/").rstrip("/") or "."
    kernel_relpath = config.get("kernel_path")
    timeout = config.get("timeout", 30)

    files_section = (
        f"- Any `.rs` file under `{algo_dir_rel}/` — the algorithm directory.\n"
        f"  - `{algo_dir_rel}/mod.rs` MUST remain and MUST contain "
        f"`fn solve_challenge(`.\n"
        f"  - You may EDIT existing siblings, ADD new ones (wire via "
        f"`mod foo;` in mod.rs), or REMOVE them.\n"
        f"- Any `.cu` file under `{algo_dir_rel}/` — CUDA kernels for GPU "
        f"challenges.\n"
    )
    if kernel_relpath and Path(kernel_relpath).parent.as_posix() != algo_dir_rel:
        files_section += f"- `{kernel_relpath}` — CUDA kernels. EDIT this if needed.\n"

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

{files_section}- `.swarm/hypothesis.json` — write your hypothesis here before stopping.

The sandbox is `workspace-write` — you technically have write access to
the whole worktree. **Do not use it.** The driver only copies files under
`{algo_dir_rel}/` back to the main checkout when scoring — any edits
outside that directory get silently discarded, so editing Cargo.toml,
src/lib.rs, or any other file is a waste of your turns and will cause
your hypothesis to underperform.

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

Strategy tags (pick the closest match): `construction`, `local_search`,
`metaheuristic`, `constraint_relaxation`, `decomposition`, `hybrid`,
`data_structure`, `greedy`, `dp`, `branch_and_bound`, `other`.

## Sandbox

- Sandbox mode: `workspace-write` (rooted at this worktree).
- Network access is DISABLED — no `curl`, `wget`, package downloads, or
  outbound HTTP. `cargo check` works because dependencies are already
  vendored/cached.
- Approval policy is `never` — there's nobody to approve prompts. If you
  hit a permission wall, work around it within these rules.

## Solver constraints

- `use super::*;` must remain the first import in the Rust file.
- `fn solve_challenge(` must remain — do not rename the entry point.
- Per-instance time budget: {timeout} seconds. The solver is killed at this
  hard deadline. Use a time-based loop (`std::time::Instant`), call
  `save_solution()` early with your first feasible solution, then keep
  improving and re-saving. The last saved solution is what gets scored.
- Do not remove `unsafe` blocks that are already there; do not add new
  `unsafe` unless you understand the invariants.

## When to stop

Stop as soon as your edit compiles AND you have written
`.swarm/hypothesis.json`. The driver runs the official benchmark after
you stop — don't run `scripts/benchmark.py` yourself, that wastes time.

## Challenge-specific details

{challenge_md}
"""


class CodexAgent:
    """Headless OpenAI Codex (`codex exec`), sandboxed to a worktree."""

    name = "codex-agentic"
    cli_name = "codex"

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
        if shutil.which("codex") is None:
            raise RuntimeError(
                "codex CLI not found on PATH. Install Codex CLI "
                "(https://github.com/openai/codex) or switch to "
                "--provider claude-code-agentic / an API provider."
            )

        last_msg_path = workdir / _LAST_MESSAGE_RELPATH
        if last_msg_path.exists():
            last_msg_path.unlink()

        cmd = [
            "codex", "exec",
            "--sandbox", "workspace-write",
            "-C", str(workdir),
            "--output-last-message", str(last_msg_path),
            "--skip-git-repo-check",
            "-c", 'approval_policy="never"',
            "-c", "sandbox_workspace_write.network_access=false",
        ]
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
