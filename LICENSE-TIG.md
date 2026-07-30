# TIG challenge code — license exception

This repository is released under the GNU General Public License v3 (see
[LICENSE](./LICENSE)), **except** for the TIG challenge and solver code, which
is derived from the [TIG Foundation monorepo](https://github.com/tig-foundation/tig-monorepo)
and remains governed by the TIG Foundation's license agreements:

- <https://github.com/tig-foundation/tig-monorepo/tree/main/docs/agreements>

The exception covers:

| Path | What |
|------|------|
| `src/` | The Rust challenge definitions and solvers (the `tig-challenges` crate) |
| `initial_algorithms/` | Per-challenge starting code and seed algorithm pools |

These directories are the *example workload* the swarm ships with. The swarm
itself — orchestration, coordination server, dashboards, runner, and CLIs — is
independent of TIG and is what the GPLv3 grant applies to. If you point the
swarm at your own (non-TIG) challenges, none of the TIG terms are involved.

Anything you submit to the TIG mainnet is additionally subject to the TIG
end-user license agreement referenced in `Cargo.toml`.
