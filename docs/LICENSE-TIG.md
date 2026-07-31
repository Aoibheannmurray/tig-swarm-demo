# TIG challenge code — license exception

This repository is released under the GNU General Public License v3 (see
[LICENSE](../LICENSE)), **except** for the TIG-derived code, which comes from
the [TIG Foundation monorepo](https://github.com/tig-foundation/tig-monorepo)
and remains governed by the TIG Foundation's licenses (full set:
<https://github.com/tig-foundation/tig-monorepo/tree/main/docs/licenses>).

Two TIG licenses apply, depending on what the code is:

1. **TIG Game Code End User License Agreement v2.0** — the challenge code
   (the `tig-challenges` crate and TIG's algorithm template):
   <https://github.com/tig-foundation/tig-monorepo/tree/main/docs/licenses/TIG_Game_Code_End_User_License_Agreement_v2.0.pdf>
2. **TIG Innovator Outbound Game License v2** — algorithm implementations
   downloaded from the TIG mainnet:
   <https://github.com/tig-foundation/tig-monorepo/tree/main/docs/licenses/innovator_outbound_license.pdf>

## What the exception covers

Under the **Game Code EULA v2.0**:

| Path | What |
|------|------|
| `src/` | The Rust challenge definitions and solver/evaluator harness — vendored from the monorepo's `tig-challenges` crate |
| `initial_algorithms/hypergraph/stub/` | GPU starting code derived from TIG's algorithm template |
| `initial_algorithms/vector_search/stub/` | GPU starting code derived from TIG's algorithm template |
| `initial_algorithms/neuralnet_optimizer/stub/` | GPU starting code derived from TIG's algorithm template |

Under the **Innovator Outbound Game License v2**: any **mainnet algorithm** a
host stages into a `stub/` slot with `scripts/download_algorithm.py` (or seeds
via `seed_inactive`) — TIG algorithm implementations remain under this license
wherever they are copied.

## What the exception does NOT cover

The initial algorithms **authored in this repository** (written with Claude,
not taken from TIG) are part of the GPLv3 grant like the rest of the repo:

- `initial_algorithms/*/seeds/` — the entire authored seed pool
  (`greedy`, `local_search`, `construction`, `brute_force`, `sgd`, …);
- the CPU `stub/` placeholders (`satisfiability`, `knapsack`, `job_scheduling`,
  `vehicle_routing`, `energy_arbitrage`) — trivial `unimplemented!()` shells
  written for this swarm.

The swarm itself — orchestration, coordination server, dashboards, runner, and
CLIs — is a challenge-agnostic harness, independent of TIG, and is what the
GPLv3 grant applies to. The TIG challenges are shipped only as the example
workload. If you point the swarm at your own (non-TIG) challenges, none of the
TIG terms are involved.

## Algorithms the swarm produces

Running the swarm on the shipped TIG challenges means compiling and using the
TIG challenge code, which the Game Code EULA v2.0 licenses *"solely for the
limited purposes of developing solutions for and submitting solutions to The
Innovation Game"* (the EULA expressly permits use *"with an LLM harness"* —
which is what this swarm is). Any algorithm produced by running the swarm
against these challenges is therefore being developed for, and/or to be
submitted to, The Innovation Game, subject to the TIG Game Rules.

That purpose limitation comes from the challenge code's license, not from the
GPLv3 harness: it applies however you drive the TIG challenges, and it does not
apply to algorithms the swarm produces for non-TIG challenges.

Anything you submit to the TIG mainnet is additionally subject to the TIG
Inbound Game License
(<https://github.com/tig-foundation/tig-monorepo/tree/main/docs/licenses/inbound_license.pdf>).
