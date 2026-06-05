# Agentic Sandbox Spec

Design doc for the sandbox that agentic providers (`claude-code-agentic`,
`codex-agentic`) run inside. Goal: one identical sandbox spec, implemented for
both backends. This file tracks **locked-in decisions** as we work through them
point by point. Implementation comes later.

Status legend: ✅ locked · 🔲 open · 💤 not yet discussed

---

## Implementation log

**Landed (Claude `claude-code-agentic` only — Codex accepted as weaker, left
as-is):** `scripts/agentic_backends.py`
- §1 read-scope: `_build_sandbox_settings` now scopes `Read/Glob/Grep` to
  `/{challenge_dir}/**` + `/CHALLENGE.md` + `/Cargo.toml` (replaced the old
  `Read(**)`), and explicitly denies `.git/`, `scripts/`, `server/`, `target/`,
  the challenge `README.md` + `baselines/`, and `*.env`.
- §4 kernel compile: added `Bash(python3 scripts/build_ptx.py:*)` to the allow
  list for GPU challenges (compiles `.cu`→PTX into the worktree's `target/ptx`;
  never executes agent code). Instruction text tells the agent to use it.
- §5 git: replaced the per-subcommand git denies with a single `Bash(git:*)`
  (covers read-only `log`/`show`/`diff` too); `.git/**` also denied for reads.
- Instruction text (`_build_claude_md`) updated to match (read-scope + kernel
  compile + self-contained-artifact note).
- §2 write logic (Edit allowlist + `Write(**)` deny + hypothesis) left UNCHANGED
  — it already matched the spec.

**Headless read behavior — VERIFIED via live `claude -p` test (claude 2.1.162),
two bugs found and fixed:**
- ❌ **Bug 1: `/`-anchored paths match nothing.** The "project-root relative"
  form `Read(/src/x/**)` that the docs/guide described silently matched no reads
  — the first implementation denied NOTHING. Working forms are **bare-relative**
  (`Read(src/x/**)`), absolute (`Read(//abs/**)`), or glob (`Read(**/x)`).
- ❌ **Bug 2: reads are DEFAULT-ALLOW.** An unlisted path is readable (confirmed:
  a file matching no allow and no deny read fine). So "omission → denied" is
  FALSE — scoping must be done by **explicit denies**, and sibling challenge dirs
  must be **enumerated** (no negation glob; can't deny `src/**` because deny >
  allow would then block the active dir too — also confirmed live: `deny
  Read(**)` blocked even the allowed in-scope files).
- ✅ **Fix landed & re-verified end-to-end** with production-generated settings:
  in-scope (`CHALLENGE.md`, `Cargo.toml`, `algorithm/mod.rs`, `kernels.cu`) read
  OK; out-of-scope (`README.md`, `baselines/`, sibling `knapsack/`, `lib.rs`,
  `main_solver.rs`, `datasets/`, `scripts/secret`, `.env`) all BLOCKED.
  `_build_sandbox_settings(config, workdir)` now uses bare-relative globs and
  enumerates non-active `src/<dir>/**` from the worktree. Also added
  `datasets/**` + `initial_algorithms/**` denies (anti-overfit/anti-anchor).

**Bash enforcement — VERIFIED via live `claude -p` test (acceptEdits, headless):**
- ✅ Allowed: `cargo fmt` (and the cargo allowlist), `python3 scripts/build_ptx.py`
  (the §4 addition works).
- ✅ Blocked: `git log` (read-only git), `curl` (network), and — critically — ALL
  shell content-readers that could bypass the Read-tool scope: `cat`, `head`,
  `tail`, `grep`, `awk`, `sed`, `base64`, `xxd` (Claude maps these to Read
  permissions, so the path denies catch them) and `python3 -c` / `sh -c`
  (arbitrary code → "requires approval" → no approver in headless → blocked).
- ⚠️ Auto-allowed but benign: `echo`, `ls` (acceptEdits auto-approves "safe"
  commands). `ls` leaks file/dir *names* (not contents) — accepted as minor.
- Net: the §1 read-scope is NOT bypassable via the shell; §3 network and §5 git
  hold at the Bash layer. Note Bash is not strict default-deny (echo/ls slip
  through), but no auto-allowed command leaks file *contents*, reaches the
  network, or executes agent code.

**Remaining follow-ups (not yet done):**
- 🔲 **§3 cargo offline/vendor** — no `.cargo/config.toml` exists; add
  `--offline`/vendored config so allowed cargo commands can't fetch (deferred
  from this pass).
- 🔲 **§2/§4 cargo build-cache isolation** — agent's `cargo` runs with cwd=
  worktree → `worktree/target` (separate from the benchmark's target). Appears
  isolated by default; confirm under Docker (`_cargo_target_volume`). PTX already
  isolated (worktree `target/ptx`).
- 🔲 **Codex worktree clean of secrets** — the exfil caveat (un-scoped Codex
  reads + writable artifact). Ensure no secrets reachable on disk from the
  worktree.

---

---

## 1. Filesystem — read access  ✅ LOCKED

**Decision:** The agent must NOT read the whole worktree (context bloat). Read
access is scoped to exactly the material needed to implement a new algorithm for
the active challenge.

**Read scope = root `CHALLENGE.md` + `src/<active-challenge>/**` (excluding
`README.md`)**, where:

| Path | Access | Rationale |
|---|---|---|
| `src/<challenge>/algorithm/mod.rs` | read **+ write** | the file it edits |
| `src/<challenge>/algorithm/kernels.cu` | read **+ write** | GPU challenges only |
| `src/<challenge>/mod.rs` | read-only | the interface contract — algorithm does `use super::*` |
| `src/<challenge>/nn/**`, other `src/<challenge>/*.rs` | read-only | helper APIs the algorithm calls (`MLP`, scenarios, utils, …) |
| `src/<challenge>/*.cu`, `*.cuh` | read-only | shared kernel headers the algorithm's `.cu` includes |
| `CHALLENGE.md` (repo root) | read-only | the objective / scoring / constraints |
| `Cargo.toml` (repo root) | read-only | available vendored crates + pinned versions + custom `cudarc` fork signal |
| `src/<challenge>/README.md` | **excluded** | redundant — identical to root `CHALLENGE.md` |
| `src/<challenge>/baselines/**` | **excluded** | avoid anchoring / copying reference implementations |

**Excluded (the bloat we're cutting):** all other challenge directories, harness
entrypoints (`main_*.rs`, `lib.rs`), `scripts/`, `server/`, `.swarm/` internals,
fleet/agent config. **Also `.git/**`** — must be unreadable so the agent can't
reach the shared object store directly (ties to §5: git read commands are denied
for the same reason).

**Key rationale notes:**
- The algorithm file is not self-contained — it starts with `use super::*;` and
  compiles against the parent `mod.rs` (`Challenge`, `Dataset`, `Solution`, the
  optimizer fn-type signatures, `solve_challenge`, `training_loop`) plus `nn/`
  helpers and external crates. The agent needs to *see* this interface to emit
  compiling code.
- Root `CHALLENGE.md` and `src/<challenge>/README.md` are **byte-identical** for
  the active challenge. We keep only root `CHALLENGE.md` because it is refreshed
  from the live server every run (so it can't drift from what's scored) and its
  path is fixed regardless of which challenge is active.

**Implementation note (important):** read-*tool* scope is separate from what's
on disk. `cargo check` needs the full crate present to compile, but the agent's
`Read`/`Glob`/`Grep` can be restricted to this subset while the whole crate
stays on disk for the compiler. Narrowing reads therefore costs nothing at build
time.

**Caveat to honor in implementation:** this assumes root `CHALLENGE.md` always
reflects the *active* challenge (true in the standalone `run_loop.py` flow,
one challenge per worktree). Revisit if a single tree ever runs multiple
challenges.

### Sub-decisions for point 1
- ✅ **`src/<challenge>/baselines/**` — EXCLUDED from read.** Avoids anchoring /
  copying reference implementations instead of innovating. (Note: this is a
  carve-out *inside* the otherwise-included challenge dir.)
- ✅ **`Cargo.toml` — INCLUDED, read-only.** Benefits:
  (1) discovers vendored-but-unused crates the agent won't see imported in the
  neuralnet code (`rand_distr`, `statrs`, `ndarray`, `rayon`, …) — and since the
  sandbox is offline, the declared deps are the entire universe of available
  crates; (2) pins versions (`rand 0.8`, `ndarray 0.15`) to the correct API
  surface; (3) flags that `cudarc` is a **custom git fork** (tig-foundation
  branch), signaling the agent not to trust its training-prior cudarc API.
  Downsides are minor: can't add deps anyway (no write access to it), and
  `optional` crates (`rayon`, `cudarc`) are feature-gated so the file slightly
  overstates availability.

---

## 2. Filesystem — write access  ✅ LOCKED

**Decision:** Agents MUST be able to edit the Rust algorithm file, the CUDA
kernel file, and `.swarm/hypothesis.json`. Agents MAY additionally create and
write **new** files (scratch/experimentation) if it helps them produce a better
algorithm — but **only the returned algorithm (algorithm file + kernel) is ever
benchmarked.** New files must not be usable to game the reward system.

### Write policy

| Target | Access | Benchmarked? |
|---|---|---|
| `src/<challenge>/algorithm/mod.rs` | edit | ✅ yes — this IS the returned artifact |
| `src/<challenge>/algorithm/kernels.cu` (GPU) | edit | ✅ yes — returned artifact |
| `.swarm/hypothesis.json` | write | ❌ no — control plane (inspiration pool/logging), can't affect score |
| **new source files** (agent-authored) | **DENIED** | n/a — inert under no-container/no-execution (see below) |
| build artifacts (`target/**`, scratch ptx) | tool-written | ❌ no — written by compile commands, not the agent |
| read-only set from §1 (`mod.rs`, `nn/**`, `Cargo.toml`, …) | **overwrite DENIED** | n/a |

So: **only the 3 files are agent-writable. No agent-created source files.**

**Why no new files (decided after §4):** in the no-container model the agent
can't *execute* anything it writes (see §4 — execution is denied). A scratch
`.rs` can't be run, and `mod`-ing it into the crate pollutes `cargo check` then
gets dropped at copy-back. New files therefore give ~zero benefit while adding
surface area, so they're disallowed. Revisit only if we adopt a container that
makes in-sandbox execution safe.

### THE security invariant (must be preserved by any implementation)

> The benchmarked artifact is exactly `{algorithm file, kernel file}`, copied by
> name into a **clean checkout** and compiled/scored there. The copy-back is a
> **strict allowlist** — it pulls those two paths, never "everything that
> changed."

This is what makes new-file creation safe. New files never reach scoring because
copy-back is a 2-file pull, not a worktree sync. **Do not** ever change copy-back
into an rsync / "copy all modified files" / git-diff-apply — that would turn
new-file creation into a code-injection vector into the benchmark.

This invariant already holds today (verified):
- `_read_worktree_files()` reads back only algo+kernel (`run_loop.py:697-708`).
- Benchmark runs in the main checkout, not the worktree: `cwd=ROOT`
  (`run_loop.py:235`), cargo build `cwd=ROOT_DIR` (`benchmark.py:204`), Docker
  bind-mounts the main repo (`benchmark.py:458`).
- No `build.rs`, no `.cargo/config.toml`, no `include!`/`include_str!` in the
  tree; scoring reads no agent-writable result/cache files — score is computed
  live from the evaluator binary's output.

### Guardrails (rationale for the policy above)

1. **Read-only set stays read-only (create-new, not overwrite).** Editing the
   harness/interface files wouldn't change the real score (not copied back) but
   *would* let the agent fake its own `cargo check`, then ship an algorithm that
   fails the official build. Keeping them read-only preserves `cargo check` as an
   honest signal.
2. **Returned algorithm must be self-contained.** The agent must NOT `mod foo;`
   or `include!("scratch.rs")` from `algorithm/mod.rs` referencing scratch files
   — they pass the worktree's `cargo check` but vanish at copy-back, failing the
   official build. The benchmarked artifact must stand alone in `mod.rs` +
   `kernels.cu`. (Communicate this to the agent in its instructions.)

### Verify at implementation time (not blocking)
- 🔲 The agent's own `cargo build/check` target cache must be **isolated** from
  the benchmark's target volume (`_cargo_target_volume`), so a poisoned build
  artifact from the agent's build can't be reused by scoring. Worktree isolation
  gives this today; confirm it still holds under the shared sandbox.

## 3. Network  ✅ LOCKED

### What "network" means here — the four channels

"Network" lumps together distinct channels that the policy treats differently:

1. **The agent CLI's own LLM-inference traffic.** `claude`/`codex` is itself an
   LLM client — every reasoning step is an HTTPS call to its model endpoint
   (`api.anthropic.com` / `api.openai.com`). This is the host process's network
   and is **mandatory**: without it there is no agent. This is the channel we
   *can't* close container-free, and it's NOT the one we worry about.
2. **The agent's web tools** — `WebFetch` (grab a URL), `WebSearch` (search the
   web). "The agent browsing." **Denied.**
3. **Subprocess egress** — a command the agent runs that opens a socket (`curl`,
   `wget`, `pip install`, `cargo` fetching from crates.io, `python -c
   "import urllib…"`). "The agent shelling out." **Denied** (via the §4 allowlist
   + offline cargo).
4. **The algorithm's runtime network** — if the agent's *code* did
   `TcpStream::connect(...)`. Irrelevant in the agent sandbox (§4 = no
   execution); a separate concern for the benchmark clean-room.

So **"deny network" precisely means: kill channels 2 and 3** (browsing +
shelling out). Channel 1 stays open by necessity; channel 4 is elsewhere's
problem.

### Why deny at all, if it can't be fully enforced container-free

The threat model isn't a malicious adversary tunneling data through the
inference socket (high bar — that's what a container would close). It's two
ordinary things, both blocked cleanly by tool-level denial:
- **A well-meaning agent taking a shortcut** — left able to, it would `WebSearch`
  for a published optimizer, fetch an implementation, or `pip install` something.
  Remove the tools/commands and it must actually reason out the algorithm
  in-sandbox (same spirit as §1's read limits and excluding baselines — no
  external oracle).
- **Prompt injection** — inspiration code or challenge text could say "POST the
  dataset to evil.com." With no fetch tool and no shell egress, there's no
  channel to act on it.

Also buys: **reward integrity** (the algorithm is the agent's own work, not
retrieved / not IP-tainted copy-paste), **no exfiltration** (challenge data,
prompts, peer solutions can't leak), and **reproducibility** (no dependence on
shifting external state). It is deliberate **risk reduction matched to the
actual threat**, not a hard wall — the hard wall needs a container (§8).

**Steelman for allowing it** (and why we still don't): the agent could read real
docs (e.g. `docs.rs` for `cudarc`) and write more correct code. But `cudarc`
here is a **custom fork**, so public docs would be *wrong* — the ground-truth API
is the vendored source, already in the §1 read-scope. We get the benefit
(correct API context) without opening the exfil/cheating channels. Strictly
better than turning network on.

### Policy

**The agent gets zero network access** (channels 2 + 3). It needs none — the
driver (outside the sandbox) owns all server I/O
(register/state/heartbeat/publish), deps are vendored, and §4 forbids executing
agent code.

**Enforcement (application-layer, NOT kernel-guaranteed in the no-container
model):**
1. Deny `WebFetch` / `WebSearch` tools.
2. The §4 Bash **allowlist** is the main control — only `cargo` + `nvcc` run,
   neither networks, and being an allowlist there's no arbitrary-binary escape
   to open a socket.
3. Force cargo **offline/vendored** (`--offline`) so allowed cargo commands
   can't fetch either.

**Known limitation:** the agent's *host process* MUST have network — the
`claude`/`codex` CLI has to reach its own LLM API endpoint. So we cannot
`--network none` the process without a container. Network denial is therefore
"the agent can't *use* the network for anything but its own inference," not a
kernel egress guarantee. A true guarantee is a §8/container item.

**Defense-in-depth:** Codex keeps its `network_access=false` OS-sandbox flag (a
stronger guarantee on its sub-commands than Claude can offer container-free). We
don't *rely* on it for the common baseline, but there's no reason to drop it.

## 4. Process / command execution  ✅ LOCKED

**Architecture decision:** stay with **today's model — no per-agent container.**
Boundary = worktree cwd + per-CLI permission lists. (Container-per-agent was
considered and deferred; see §8.) Agents do **not** self-benchmark — that's the
official benchmarking stage's job.

### Governing principle (forced by the no-container choice)

> **Commands may COMPILE or ANALYZE the agent's code, but must never EXECUTE
> it.**

In the no-container model the permission list only governs the agent's *tool
calls*, not a process once it starts. Any executed binary runs with the host
user's full privileges — it can open network sockets, and `std::fs` to absolute
paths reaches the whole host, not just the worktree. So executing agent-authored
code (`cargo run`, `cargo test`, the solver/benchmark, a scratch binary) is a
straight sandbox escape. Compile/lint/format/PTX-compile never run agent code →
safe. This is exactly why the existing allowlist is the four non-executing cargo
commands.

### Allowed (non-executing)

| Command | Purpose |
|---|---|
| `cargo check` | type-check (fast feedback) |
| `cargo build` | full compile of the Rust crate |
| `cargo fmt` | formatting |
| `cargo clippy` | lints / correctness (high value for Rust) |
| **kernel PTX compile** — `build_ptx.py <challenge>` (or `nvcc --ptx`) | **NEW.** compile-check `kernels.cu` → PTX |

**Why the kernel-compile addition matters:** `kernels.cu` is NOT compiled by
cargo — it's turned into PTX separately by `scripts/build_ptx.py`
(`benchmark.py:517`) and loaded at runtime via NVRTC (`Ptx::from_src`,
`main_gpu_benchmark.rs:348`). So `cargo check`/`build` give the agent **zero**
feedback on kernel edits today; a syntax error only surfaces at official-
benchmark time. Allowing the agent to run the PTX compile (compile-only, no
execution) closes that gap and mirrors the official compile step. `nvcc` is
present in the GPU image (`nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04`).

### Denied

- **Executes agent code:** `cargo run`, `cargo test`, `cargo bench`, the
  solver/evaluator/benchmark binaries (`main_solver`, `main_gpu_benchmark`,
  `benchmark.py`), any scratch binary.
- **Network:** `curl`, `wget`, `nc`, `ssh`, `scp`, `rsync`, … (and see §3).
- **Git state/remote:** `push/pull/fetch/clone/commit/checkout/add/...`, `gh`.
- **FS mutation / privilege:** `rm`, `mv`, `cp`, `chmod`, `chown`, `dd`, `mkfs`,
  `sudo`.

### Optional / not adopted (keep allowlist tight)
- `cargo tree`, `cargo doc --no-deps`, `cargo expand` — non-executing and
  harmless, but the agent's Read/Grep over vendored source covers most of it.
  Left out unless agents prove to need them.

### Verify at implementation time
- 🔲 Kernel PTX compile must write to the **worktree's** `target/ptx` (or a
  scratch dir), isolated from the benchmark's target volume (ties to the §2
  build-cache-isolation item). Confirm `build_ptx.py`'s `--outdir` can be
  redirected and that it performs no execution beyond `nvcc`.

## 5. Git access  ✅ LOCKED

**Policy: deny git entirely — including read-only git, not just mutation.**

**The non-obvious reason read-only git must also be denied:** a git worktree
shares the main repo's object store, so `git show HEAD:src/other_challenge/...`,
`git log`, `git diff`, `git cat-file` would let the agent **dump any blob or
history in the whole repo** — bypassing the entire §1 read-scope. Restricting
filesystem reads is pointless if `git show` is an open door into the object
store. So read-only git is as off-limits as `git push`.

The agent has no legitimate git need: the driver owns the worktree lifecycle,
branches, and copy-back (which reads files off disk, not via git).

**Enforcement:**
1. **All git denied** (mutation AND read). Falls out of the §4 allowlist for
   free (git isn't `cargo`/`nvcc`). Keep an explicit `git *` / `gh` deny as
   belt-and-suspenders and for the Codex side.
2. **`.git/**` outside the §1 read allowlist** so the agent can't read objects
   directly via its Read tool either. (Already true if §1 is a true allowlist.)

---

### Cross-cutting linchpin (applies to §3, §4, §5)

§3 and §5 are mostly *consequences* of making §4 a **strict allowlist** rather
than a denylist. Implementation requirement: on the Claude side, Bash must be
**default-deny** (only allowlisted prefixes run). The current code mixes allow +
an explicit denylist precisely because Claude's Bash may default-permissive
(comment at `agentic_backends.py:144`). Target allowlist semantics (unlisted =
denied); fall back to the explicit denylist only if default-deny isn't
achievable. Also add `.git/**` to the §1 read exclusions.

## 6. Resource limits  ✅ LOCKED

**Decision: wall-clock timeout only** — the existing `--agentic-timeout`
(default 1800s / 30 min), which bounds the whole iteration.

Rationale: hard CPU/memory/disk/PID caps are cgroup features that require a
container; in the no-container model (§4/§8) we can't kernel-enforce them, so we
don't pretend to. Wall-clock is the one bound we *can* enforce (the driver kills
the subprocess on timeout). Revisit if we adopt a container (§8), which would
make real resource caps cheap.

## Per-harness feasibility  ✅ DECIDED

Root cause of every Claude/Codex difference: **Claude = per-tool/per-path
permission engine** (fine-grained, default-deny achievable); **Codex = coarse
OS-sandbox modes** (`workspace-write` + network flag) with **no per-path or
per-command discrimination**.

| # | Claude | Codex |
|---|---|---|
| 1 Read-scope | ✅ enforce (deny patterns; files stay on disk for cargo) | ❌ whole worktree readable; can't strip files (cargo needs them) |
| 2 Write-scope | ✅ Edit allowlist + deny Write | ⚠️ not at write-time; ✅ via copy-back |
| 3 Network | ✅ app-layer | ✅ OS-layer (stronger) |
| 4 Cmd allowlist / no-exec | ✅ Bash allow-patterns | ❌ no command filter; can execute (but OS-contained) |
| 5 Git deny (incl read) | ✅ | ❌ local `git show`/`log` possible |
| 6 Wall-clock | ✅ driver | ✅ driver |
| 7 Violation handling | ✅ hard-refuse | ⚠️ instruct + drop + OS-contain |
| 8 No container | ✅ | ✅ |

**DECISION: accept Codex as the weaker backend for now.** Security still holds on
both (rests on §2 copy-back + §3 net-sandbox). On Codex, §1/§4/§5 degrade to
advisory + OS-containment + copy-back backstop. Not pursuing the parity fix
(minimal de-gitted per-challenge worktree) at this time.

**Caveat to watch on Codex:** un-scoped reads + writable copied-back artifact =
a narrow exfil channel (read an on-disk secret → embed in `algorithm/mod.rs` →
published). Mitigation: ensure **no secrets are reachable on disk** from the
worktree. (Claude's read-scope closes this; Codex relies on a clean disk.)

---

## 7. Failure / violation behavior  ✅ LOCKED

**What this covers:** when the agent attempts a forbidden action (edit a
read-only file, run a denied command, create a new file, `WebFetch`, …), (A) what
happens at action time, and (B) what happens to the iteration.

### Decision

1. **Hard, visible refusal at action time — wherever the backend can enforce
   it.** The agent gets a "permission denied" it can see and self-correct from.
   Silent drops are bad: the agent thinks it succeeded and burns its remaining
   budget building on an illusion. (Claude's native per-tool behavior; Codex's OS
   sandbox also hard-errors on the channels it covers — network, exec.)
2. **Never abort the iteration over a violation.** A refused action is
   recoverable — let the agent continue and still score whatever valid algorithm
   it produced. Aborting would throw away good work over one bad tool call.
3. **Accept one unavoidable asymmetry (file-scope).** Codex's `workspace-write`
   can't enforce per-file write-scope, so for "you edited the wrong file" Codex
   can't give a live hard error — it falls back to `AGENTS.md` instructions +
   silent drop at copy-back. Claude *can* hard-refuse there. Perfect parity on
   file-scope is not achievable container-free.

### Crucial framing
§7 is **NOT a security layer.** Security is guaranteed by §2 (clean-room
benchmark + allowlist copy-back), which holds regardless of what the agent does
— even a silently-dropped violation can't reach scoring. §7 is purely about
**agent productivity**: fast, honest feedback so the agent doesn't waste its
iteration.

**Summary:** hard-refuse-and-surface where the backend can; instruct + drop
where it can't; never abort; security doesn't depend on it.

## 8. Enforcement guarantee level  ✅ LOCKED

**Decision: NO container for now — keep today's model.** Enforcement stays
in-process and per-backend:
- Hard boundary = the git worktree (agent's cwd) — physical isolation from
  sibling agents, secrets, the main checkout, `$HOME`.
- Claude: `.swarm/sandbox-settings.json` permission lists, enforced by the
  `claude` CLI per tool call (target: default-deny Bash — see §5 linchpin).
- Codex: `workspace-write` OS sandbox + `network_access=false` + `AGENTS.md`
  soft instructions, with the copy-back allowlist as the file-scope backstop.

**Accepted consequences** (documented, not bugs):
- Network denial is application-layer, not a kernel egress guarantee (§3).
- Resource limits are wall-clock only, no cgroup caps (§6).
- File-scope enforcement differs by backend: Claude hard-refuses at edit time;
  Codex relies on instructions + copy-back drop (§7).

**What would flip this:** wanting the agent to *execute* its code (self-
benchmark, run experiments, `cargo test`). That requires a per-agent container
to be safe (kernel-enforced network + fs + resource isolation), at which point
§2/§3/§4/§6 all relax. Until then, the no-container model holds and the security
guarantee rests on §2 (clean-room benchmark + allowlist copy-back), not on the
in-process permission lists.
