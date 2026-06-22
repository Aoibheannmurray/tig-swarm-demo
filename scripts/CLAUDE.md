# CLAUDE.md — scripts/ (orchestration)

The Python that drives the swarm locally: the per-agent improvement loop, the
multi-agent fleet, benchmarking, and publishing results to the server.

## Gotchas — read before refactoring

- **This is NOT a package.** Modules are flat, have no `__init__.py`, and import
  each other by **bare name** (`from prompts import ...`, `from agentic_backends
  import ...`) via `sys.path`. Don't nest files into subfolders or switch to
  relative imports without rewiring every import.
- **Several modules run as subprocesses by path** — e.g. `python
  scripts/run_loop.py`, and `benchmark.py` is invoked with `cwd=<worktree>`.
  Renaming or moving a module breaks those call sites; grep the filename first.
- `setup.py` is at the **repo root** (host-admin CLI), not here. Scripts shell
  out to it as `python setup.py sync`.

## Map

- `run_loop.py` — one agent's loop: sync config → get state → propose an edit →
  `benchmark.py` → `publish.py`. The core driver.
- `run_fleet.py` — launches/manages N agents (one git worktree each) from
  `fleet.config.json`. The root `run.py` wraps this for contributors.
- `benchmark.py` — compiles + scores an algorithm (local Docker or C3 cloud).
  Run with `cwd` = the agent's worktree.
- `publish.py` — posts score + hypothesis + heartbeat to the server.
- `agentic_backends.py` / `agentic_sandbox.py` — sandboxed coding-agent mode
  (Claude Code / Codex editing in a worktree). **`agentic_backends.py` generates
  the per-worktree `CLAUDE.md` / `AGENTS.md` and permission settings** — see
  `_build_sandbox_settings`.
- `llm_backends.py` / `prompts.py` — single-shot (non-agentic) LLM mode.
- `c3_compute.py` — cloud GPU compute (C3) path for benchmarking.
- `challenge_files.py`, `download_algorithm.py`, `init_fleet.py`,
  `sync_identity.py`, `build_ptx.py` — supporting helpers.

## Tests

No pytest. `test_*.py` are self-running scripts (`if __name__ == "__main__"`) —
run one directly, e.g. `python scripts/test_benchmark_run_ids.py`.
