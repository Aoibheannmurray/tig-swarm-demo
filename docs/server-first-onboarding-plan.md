# Server-first onboarding & runner tiers — design

Status: **draft for review** · 2026-07-08
Companion to [ARCHITECTURE.md](./ARCHITECTURE.md) (how the system behaves today).

## 1. Summary

Move the contributor control plane from the contributor's machine to the
hosted swarm server, so joining a swarm becomes: **click a join link → paste
an API key → watch your agents on the dashboard**. The local footprint shrinks
from "clone a repo, export env vars, answer a wizard" to — depending on tier —
nothing at all, or a single pasted command.

Three contribution tiers, built in order:

| Tier | Who it serves | Local footprint | Status quo replaced |
|------|---------------|-----------------|---------------------|
| **1 — hosted fleet** | novices; API-mode + C3 agents | none (browser only) | the whole clone/wizard flow |
| **2 — local runner** | Docker / agentic-CLI / trust-sensitive users | one pasted command | the wizard (config comes from the server) |
| **3 — desktop app** | novices who need local compute (rare) | signed download | deferred until demanded; see §13 |

Nothing here removes the existing CLI/wizard flow; every phase is additive.

## 2. Goals

- One join link carries everything a contributor needs (server URL +
  credentials). No hand-pasting of three separate values.
- Fleet configuration (agents, providers, models, roles) authored and stored
  **server-side**, editable from a hosted UI, consumed by whichever runtime
  executes the fleet.
- A zero-install tier: contributors who choose API-mode LLMs + C3 benchmarking
  can contribute without installing anything.
- Keys handled without `export`: local secrets file for Tier 2, encrypted
  server-side store for Tier 1 (opt-in, clearly labeled).
- Zero new hosting spend: everything ships on the existing Railway server (+
  optionally one sibling service for Tier 1). No code-signing costs.

### Non-goals (this plan)

- No desktop app build (that's a later, evidence-driven decision — §13).
- No change to agent-facing APIs (`/api/state`, `/api/iterations`, register,
  tokens) or to scoring/trajectory mechanics.
- Host provisioning is a prerequisite, not a phase: every tier assumes a
  Railway swarm server already exists, and creating one stays on the host's
  machine in P0–P3 (Railway credentials are host-local by design). It is
  already UI-driven there, not terminal-only — and §9 sketches the optional
  browser-only "Deploy on Railway" path.
- Not a multi-swarm account system; credentials remain per-swarm.

## 3. Current state (what we build on)

- **Auth**: contributors authenticate `/api/agents/register` with
  `X-Username` + `X-Swarm-Password` headers. The per-contributor password is
  `sha256(username + ':' + base_password)`; the server stores only the base
  (`server/server.py:343` `_derive_user_password`, `:369`
  `verify_swarm_password`). Revocation is a username list in config; failed
  attempts are rate-limited. Registered agents already record
  `contributor_username` (`server/server.py:775`), so per-contributor views
  need no schema change.
- **Invites**: `python setup.py invite [<name>]`
  (`hostadmin/contributors.py:run_invite`) derives the password, records the
  username server-side, and prints values for out-of-band sharing. The local
  companion exposes the same derivation
  (`control_server.py` "Invite (host, local)").
- **Fleet config**: `fleet.config.json` on the contributor's disk — top-level
  `server_url` / `username` / `swarm_password` + an `agents` array
  (`scripts/run_fleet.py:_load_fleet`). LLM keys are **env-var names**
  (`api_key_env`), resolved from the process environment. C3 keys come from
  `C3_API_KEY` or a raw `c3_api_key` config value.
- **Hosted UI precedent**: the server already serves the dashboard (static
  mount, `server/server.py` tail) and the admin console — a control-ui
  (Svelte) build shipped in the server image and served at `/admin/`
  (`control-ui/README.md`, mode `hosted`). Adding hosted contributor pages
  follows an existing pattern, not a new one.
- **Server image constraint**: the production Dockerfile copies **only**
  `server/` + built frontends (`server/CLAUDE.md`). Server-side code for this
  plan must live in `server/` and must not import from `scripts/` or the repo
  root. Anything that needs the full repo (git worktrees, `run_loop.py`, the
  `c3` CLI) belongs in the separate Tier-1 runner service (§8), not the
  coordination server.
- **Boot-time self-configuration**: on the first boot of a fresh DB the
  server applies swarm config from environment variables
  (`server/db.py:_apply_env_swarm_config`), with `INSERT OR IGNORE` defaults
  behind it; `setup.py create` sets those vars and *also* pushes config over
  the admin API as belt-and-braces (`hostadmin/swarm.py:push_config_to_server`).
  Load-bearing for §9: a deploy that only sets env vars already boots
  configured.

## 4. Target architecture

```
                    ┌──────────────────────────────────────────────┐
                    │  Hosted swarm server (existing Railway app)  │
                    │  • scoreboard + APIs (unchanged)             │
                    │  • dashboard (existing)   • /admin (existing)│
                    │  • /join page             (P0, new)          │
                    │  • contributor console    (P1, new)          │
                    │  • contributor config API (P1, new)          │
                    └───────┬──────────────────────────┬───────────┘
                            │                          │
        Tier 2: local runner│                          │Tier 1: hosted runner
                            ▼                          ▼
        ┌───────────────────────────┐   ┌───────────────────────────────┐
        │ contributor's machine      │   │ "runner" service (sibling     │
        │ run.py --join <link>       │   │  Railway container, P3)       │
        │ config: fetched from server│   │ repo + git + python + c3 CLI  │
        │ keys: local secrets file   │   │ keys: encrypted at rest       │
        │ compute: local Docker / C3 │   │ compute: C3 only              │
        └───────────────────────────┘   └───────────────────────────────┘
```

The wizard's job (collect provider/model/count/role) moves into the hosted
contributor console. The runner — local or hosted — becomes a dumb executor:
*join token in, config out, run.*

## 5. P0 — join links

**Link format**: `https://<server>/join#u=<username>&p=<derived_password>`

Credentials ride in the **URL fragment**, which browsers never send to the
server — they stay out of Railway/proxy logs. The link is exactly as secret
as the values the host already shares out-of-band today; this changes the
packaging, not the trust model. No server-side invite state is needed for P0
(single-use/expiring invites are a later upgrade, §14).

Work items:

1. `hostadmin/contributors.py:run_invite` — additionally print the join link
   (it already resolves the server URL for the recorded-contributor POST via
   `_resolve_host_server_url`). Same for the local companion's invite
   endpoint so the host UI shows a copyable link.
2. **`/join` page** (new page in the control-ui hosted build, served like
   `/admin/`): reads the fragment, validates credentials against the server,
   stores them in `localStorage` (keyed by origin), then presents the tier
   choice:
   - *"Run my agents in the cloud"* — Tier 1 when available; hidden until P3.
   - *"Run on my machine"* — shows the Tier-2 one-liner with the join link
     embedded, plus copy button and OS tabs. Until P2 lands, this shows
     today's clone + `run.py --ui` instructions with the credentials
     pre-filled for pasting.
   - *"Manual / power user"* — collapsible: the raw three values +
     `fleet.config.json` instructions (today's flow, unchanged).
3. **`GET /api/contributor/me`** (new, `Depends(verify_swarm_password)`):
   returns `{username, swarm_name, active_challenge}`. Lets the join page
   show "✓ valid invite for *alice* — swarm is optimizing *knapsack*" and
   lets every later contributor-console call reuse the same auth check.

Ship-alone value: even with nothing else built, invites become one link
instead of three pasted values, and the join page teaches the current flow.

## 6. P1 — hosted contributor console + server-stored fleet config

**Storage**: new table
`contributor_configs(username TEXT PRIMARY KEY, config_json TEXT, updated_at TEXT)`.

**API** (both `Depends(verify_swarm_password)`):

- `GET /api/contributor/config` → `{config, updated_at}` (404 → UI offers a
  starter config).
- `PUT /api/contributor/config` — validated: agents array with the same
  fields `fleet.config.json` accepts today (`name`, `provider`, `model`,
  `api_base`, `compute`, `hardware`, `role`, `detailed_prompts`,
  `tacit_write`, HPO/cleaner knobs, …). **Hard-reject any raw secret
  fields** (`c3_api_key`, anything matching `*_key`/`*_token` values):
  Tier-2 configs stored on the server must contain env-var *names* only.

**Console UI** (contributor-facing pages in the hosted control-ui build):

- Agent list with add/edit/remove — the same decisions the wizard makes
  today. The tier-derived defaults (`frontier → explorer`,
  `standard → detailed_prompts`) currently live in
  `scripts/init_fleet.py:_build_agent`; the server can't import that
  (§3, self-contained image). **Decided:** the provider catalog is
  duplicated as `server/providers.json` (served at `GET /api/providers`)
  with `scripts/test_provider_catalog_parity.py` as the drift alarm, and
  the tier logic is computed server-side from the existing
  `server/tiers.py` (`GET /api/contributor/agent_defaults`).
- "My agents" status strip: the dashboard state filtered by
  `contributor_username` — data the server already has.
- Per-contributor tacit-knowledge editor (stored alongside the config; the
  runner materializes it into the workspace — replaces the local
  `tacit_knowledge.md` for server-configured fleets; local-file flow
  unchanged for classic runs).

The join page's "Run on my machine" card now ends with: *"1. configure your
agents here → 2. run this one command."*

## 7. P2 — runner join mode (Tier 2)

**Command**: `python3 run.py --join "<join-link>"`

1. Parse the fragment → write a minimal `fleet.config.json`:
   `{server_url, username, swarm_password, "config_source": "server"}`.
2. When `config_source == "server"`, `run_fleet._load_fleet` fetches
   `GET /api/contributor/config`, merges it, and caches it
   (`.fleet-cache.json`, gitignored) so restarts work offline / mid-outage.
   Precedence: a local `agents` array in `fleet.config.json`, if present,
   wins (escape hatch + full back-compat).
3. **Local secrets, no `export`**: for each configured provider whose
   `api_key_env` is absent from the environment, prompt once and store in a
   gitignored, `0600` `secrets.local.json`; inject into the fleet process
   env at spawn (`run_fleet` / `control_server`). Env vars keep precedence.
   The `--ui` companion gets read/write endpoints + a keys page for the same
   file (loopback-only server and DNS-rebinding guard already protect this
   surface). This also fixes today's biggest terminal dependency for
   *existing* users.
4. Config refresh: `run_fleet`'s monitor re-fetches the server config on the
   existing hot-reload cadence, so edits in the hosted console propagate to
   a running local fleet the same way `role` edits do today.

Later packaging (`uvx tig-swarm join <link>` / `docker run tig/runner
<link>`) wraps this same entry point; the launcher-managed-checkout design
from the earlier app investigation applies, but is out of scope here.

## 8. P3 — hosted fleet runner (Tier 1)

A **separate service** in the same Railway project (`runner/`), built from
the full repo image (python + git + `c3` CLI + repo checkout). The
coordination server stays self-contained and never holds raw LLM keys.

- **Enablement flow**: contributor picks "run in the cloud" in the console →
  pastes LLM + C3 keys into a write-only form (C3 keys are web-mintable at
  `cthree.cloud/dashboard/settings` — link it) → runner stores them encrypted
  (Fernet; key from a Railway env secret, e.g. `RUNNER_SECRET_KEY`) in its
  own SQLite on its own volume. UI thereafter shows provider + key last-4
  only. Delete = destroy row + stop fleet.
- **Execution**: the runner supervises one fleet per enrolled contributor —
  per-contributor workspace, per-agent git worktrees, `run_loop.py`
  subprocesses with the decrypted keys injected into child env only. This
  generalizes `control_server.py`'s `FleetManager` (today: exactly one
  foreground fleet) into a keyed multi-fleet supervisor — the main new code.
- **Hosted fleets are C3-only** (validated at enrollment): no Docker in the
  runner container, and LLM-generated code is therefore never *executed* on
  the runner box — agents only edit text, call APIs, and submit C3 jobs, so
  per-contributor OS-level sandboxing is not required for P3. (Agentic
  providers are also excluded from Tier 1: they need interactive CLI logins.)
- **Caps**: `max_hosted_agents_per_contributor` and a global ceiling, set by
  the host; enrollment beyond the cap is rejected with a "run locally
  instead" pointer.
- **Cost attribution**: LLM spend on the contributor's LLM key, benchmark
  spend on the contributor's C3 key; the runner burns only orchestration CPU
  (the loops are HTTP-bound).
- **Revocation**: `setup.py revoke` already blocks registration and stops
  agents server-side; add a runner hook so revoke also tears down the hosted
  fleet and deletes stored keys.
- **Observability**: runner keeps a per-fleet log ring the console polls
  (`GET /api/runner/logs`); the dashboard remains the scoreboard.
- **Discovery + CORS**: the coordination server advertises the runner via a
  `runner_url` config key, surfaced in `/api/contributor/me`; the join page
  shows the cloud tier only when it's set. The browser calls the runner
  cross-origin with the contributor header pair, so the runner allows the
  coordination origin via CORS (custom headers, no cookies).

Implemented as the `runner/` package (`runner.service:app`): `vault`
(Fernet), `store` (own SQLite), `validation` (C3-only / no-agentic / caps),
`auth` (delegates to `/api/contributor/me`), `supervisor` (keyed multi-fleet,
`git clone --local` isolation, injectable launcher). See `runner/README.md`.

## 9. Host provisioning: what this plan assumes, and the browser-only path

Everything in §5–§8 assumes a provisioned swarm server. Creating one is a
**host-machine** operation today and stays that way through P0–P3 — but it is
already UI-driven, not terminal-locked:

- `python3 run.py --ui` → the Host surface drives the full deploy
  (`hostadmin/swarm.py:create_swarm`: Railway provision → env vars → volume →
  deploy → domain → push config → seed pools → write local files), with a
  workspace picker, per-challenge instance editor, and in-UI deploy errors.
- The residual terminal steps are installing the Railway CLI and running
  `railway login` (a browser OAuth kicked off from the CLI) — one-time,
  host-only, and a far smaller ask than what contributors face today.

**Optional endgame — "Deploy on Railway" button (P5).** Railway templates
can define the service, volume, and env vars — including generated secrets —
from a repo, entirely in the browser. Because the server self-configures
from env on first boot (§3), a template deploy already yields a working,
configured swarm. Closing the remaining gaps would make host setup fully
browser-only:

1. **Seed pools**: `setup.py create` reads `initial_algorithms/` from the
   host's clone and POSTs `/api/admin/seed_pool`. A template deploy has no
   clone — bake the seed sources into the server image and seed on first
   boot (they are small text files), or add a "seed now" action to the
   hosted admin console.
2. **Per-challenge config**: the env blob (`encode_challenges_blob`) must be
   optional — first boot falls back to the `INSERT OR IGNORE` defaults, and
   the admin console (which already exists hosted) becomes the place to tune
   challenges post-deploy, replacing `setup.py switch` for button-deployed
   swarms.
3. **Credential surfacing**: the generated `admin_key` / base
   `swarm_password` live in Railway's variables view; the admin console's
   first-login flow should tell the host where to find them and immediately
   offer join-link creation (§5), so "create swarm → invite a friend" never
   leaves the browser.

This is deliberately last: it's independent of the contributor tiers, and
the template must track repo releases (a maintenance commitment), so it
should land once the hosted surfaces it depends on (§5, §6, admin console
config editing) are stable.

## 10. Security notes

- Join fragments keep credentials out of server/proxy logs; the link is
  handled like the password it wraps (the invite message should say so).
- Contributor-console credentials live in `localStorage` per swarm origin —
  same exposure class as the admin console today; keep the existing CSP/XSS
  hygiene (see the `ui: fix XSS sink` hardening commit) on all new pages.
- The derived-password and revocation model is unchanged; `/api/contributor/*`
  reuses `verify_swarm_password`, inheriting its rate limiting.
- Tier-1 key custody is the plan's one real trust expansion. Mitigations:
  opt-in with explicit copy ("the host's server stores your keys —
  use spend-limited keys"), encrypted at rest, write-only UI, decrypt only
  into child-process env, delete-on-disable, revoke-tears-down. Recommend
  OpenRouter (per-key spend limits) in the UI for Tier 1.
- Config PUT validation must reject secrets so contributors can't
  accidentally persist raw keys into `contributor_configs` (§6).

## 11. Compatibility & migration

- All phases are additive. The wizard, `fleet.config.json` hand-editing,
  `run.py --ui`, and `scripts/run_fleet.py` keep working unchanged.
- `setup.py invite` output gains a link; the raw values remain printed.
- The local companion keeps the host-provisioning surface for as long as
  hosts deploy from their machines (Railway creds are host-local; §9's P5
  button is the only thing that could eventually supersede it). Its
  *contributor* pages become redundant after P2 and can thin out later — no
  removal in this plan.
- Docs: README "Contributor" section gains the join-link path as the primary
  flow once P2 lands; existing sections stay as the manual flow.

## 12. Phasing

| Phase | Deliverable | Size | Ships value alone? |
|-------|-------------|------|--------------------|
| P0 | join link: invite prints URL, `/join` page, `/api/contributor/me` | S | yes — 1 link replaces 3 pasted values |
| P1 | contributor console + `contributor_configs` + config API + tacit editor | M | yes — config authored once, visible fleet status |
| P2 | `run.py --join`, server-config fetch + cache, local secrets file (+ keys UI in companion) | M | yes — one-command setup; kills `export` for everyone |
| P3 | hosted runner service (multi-fleet supervisor, encrypted keys, caps, revoke hook) | L | yes — zero-install tier |
| P4 | PWA manifest for the hosted join page; no-clone contributor packaging (curl bootstrap `deploy/get-swarm.py` + `Dockerfile.contributor`) | S–M | cosmetic / packaging |
| P5 *(optional)* | "Deploy on Railway" (`deploy/DEPLOY_ON_RAILWAY.md`) + first-boot seeding (`server/first_boot.py`, image-baked snapshot) | M | yes — browser-only host setup |

**P4/P5 note — `uvx` dropped.** Standard Python packaging (needed for
`uvx tig-swarm …`) is incompatible with this repo: the load-bearing root
`setup.py` is a host CLI, not setuptools, and the build backend executes it
during a wheel build (it imports `hostadmin`, absent in an isolated build
env → build fails). The no-clone goal is met instead by a stdlib curl-pipe
bootstrap (`deploy/get-swarm.py`, managed checkout → `run.py`) and a
contributor Docker image — neither needs the repo to be a package.

Suggested order is strict for P0–P3: each phase stands on the previous one's
auth and config plumbing. P5 is independent and can land any time after P1.

## 13. The desktop app, revisited

Deliberately deferred. Tier 1 serves novices; Tier 2's audience owns a
terminal. An unsigned app is novice-hostile on macOS 15 (Settings-buried
"Open Anyway"), and signing costs money the project doesn't want to spend.
If demand materializes, the earlier launcher design (thin shell + managed
checkout + bundled uv/git) sits cleanly on top of P2's `--join` runner: the
app is then "Tier 2 in a window", and nothing in this plan is thrown away.

## 14. Open questions

1. ~~**C3 key minting without a terminal**~~ **Resolved:** contributors can
   create/manage API keys from the C3 web dashboard at
   `https://cthree.cloud/dashboard/settings` (docs: *"Create and manage API
   keys for programmatic access"*) — onboarding copy links there; no `c3
   login` needed to obtain a key. Job submission and data transfer remain
   **CLI-only** (the dashboard explicitly excludes `c3 deploy` / `c3 data
   cp`), so the `c3` binary stays required wherever fleets execute — Tier-2
   machines and the Tier-1 runner image alike. Still open: whether a Windows
   `c3` binary exists (`install.sh` is sh-only) — affects Tier-2 Windows
   guidance.
2. **`c3` CLI in the runner image** — redistribution/licensing and version
   pinning (`cthree.cloud/install.sh` at build vs vendored binary).
3. ~~**Provider/tier defaults sharing**~~ **Resolved** (§6):
   duplicated-with-test — `server/providers.json` +
   `scripts/test_provider_catalog_parity.py`; no build step.
4. **Invite hardening** — upgrade P0's stateless links to server-side
   single-use/expiring invite tokens (new table + redemption endpoint)?
   Cheap once `/join` exists; decide after P0 usage.
5. **Frontend consolidation** — contributor console lives in the control-ui
   (Svelte) hosted build per the `/admin/` precedent; folding the dashboard
   (TS/Vite) into one app is attractive long-term but out of scope.
6. **Runner placement for self-hosters without Railway** — the runner should
   also run bare (`python runner/service.py`) next to a self-hosted server;
   keep it container-optional.
7. **Railway template mechanics (P5)** — confirm template support for the
   volume + generated secrets this deploy needs, and how template updates
   track repo releases (pin to a release branch/tag). Also decide the
   first-boot seeding trigger (image-baked seeds vs admin-console action).
