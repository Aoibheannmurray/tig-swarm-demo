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
- `swarm_client.py` — HTTP client for the coordination server (register, state,
  heartbeat, publish, `resolve_server_url`). Named to avoid shadowing the
  top-level `server/` package on `sys.path`.
- `c3_compute.py` — cloud (C3) path for benchmarking.
- `challenge_files.py`, `download_algorithm.py`, `init_fleet.py`,
  `sync_identity.py`, `build_ptx.py` — supporting helpers.

## TIG-docker benchmark backend

An alternative to the custom benchmark path that compiles + scores in the **real
TIG toolchain** (fuel-instrumented; `tig-runtime`/`tig-verifier`), gated by
`benchmark_backend: "tig"` (or env `TIG_BENCH_BACKEND=tig`). Both `benchmark.py`
(local docker) and `c3_compute.py` (C3) branch into it via `_tig_backend(cfg)`.

- `modified_test_algorithm` — fuel-capturing tester (additive copy of the
  monorepo `test_algorithm`); `--output-json` emits per-nonce records + aggregates.
- `tig_bench_driver.py` — runs inside the image: `build_algorithm` + per-track
  `modified_test_algorithm` → one combined JSON.
- `build_bench_image.sh` — builds the custom image from `../tig-monorepo` + the pin.
- Repo root: `Dockerfile.bench`, `tig_pin.json` (pinned TIG version);
  `docs/tig_docker_plan.md` (design + status). Algorithms author against
  `tig_challenges::<ch>::*` (`src/lib.rs` self-aliases the crate as `tig_challenges`).

## Tests

No pytest. `test_*.py` are self-running scripts (`if __name__ == "__main__"`) —
run one directly, e.g. `python scripts/test_benchmark_run_ids.py`.
