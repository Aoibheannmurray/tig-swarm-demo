# control-ui — Prometheus swarm setup UI

A sleek web UI in the Prometheus design system that replaces the terminal
wizards for standing up and joining a swarm. The classic CLI
(`python setup.py …`, `python run.py`) still works as a fallback.

## Three surfaces, one app

Svelte + Vite, no framework beyond Svelte. Three build entries:

- `index.html` → **local companion** (mode `local`). Contributor join + fleet
  monitor, and host create/switch. Served by the local `control_server.py`,
  which it reaches via `/local-api/*`.
- `admin/index.html` → **Admin Console**. Five tabs: contributors
  (invite / revoke / list), challenge switch, broadcast, seed pools, and
  settings (swarm / HPO knobs, failed-attempts archive). Served in two
  places: by the local companion at `/admin/` (the host's default route —
  it can also reach `/local-api`, which powers the authored-pool re-seed),
  and by the swarm's own hosted FastAPI server at `<server>/admin/` for
  admin work away from the host machine. Talks to `/api/*` + `/api/admin/*`
  (admin key kept in `sessionStorage`); the companion reverse-proxies those
  to the swarm.
- `join/index.html` → **hosted join page**. The doorway a one-link invite
  opens: validates the credentials in the URL fragment and hands the
  contributor one per-OS command that opens the local companion.

Design tokens are copied verbatim from `dashboard/src/style.css` into
`src/tokens.css` — keep them in sync if the dashboard palette changes.

## Run

```bash
# 1. the local companion (serves the built bundle + /local-api)
python control_server.py          # or: python run.py --ui

# 2. dev with hot reload (proxies /local-api to control_server on :8787)
cd control-ui && npm install && npm run dev
```

Build: `npm run build` (`svelte-check` + `vite build` → `dist/`).
Fast build (no type-check): `npm run build:fast`. Test: `npm test` (vitest).

**`dist/` is committed**, and every build's `postbuild` hook writes
`dist/.buildstamp` (a hash of the sources, via `scripts/ui_buildstamp.py`).
CI fails when the stamp doesn't match the sources — after editing anything
under `src/`, rebuild and commit `dist/`. Merge conflicts under `dist/` are
resolved by rebuilding from the merged sources, never by picking a side.
`control_server.py` auto-rebuilds a stale bundle at startup when npm is
available.

## How it ships

- Local companion: `control_server.py` serves `control-ui/dist` from disk.
- Hosted surfaces: the root `Dockerfile` builds this and copies `dist/admin`,
  `dist/join`, `dist/assets`, `dist/fonts` and the icon/font/manifest files
  into the server image's `static/`, so the swarm server serves the console
  at `/admin/` and the join page at `/join/`. (The companion's
  `dist/index.html` is deliberately NOT copied — it would shadow the
  dashboard's own `index.html`.)

## Backend contract

`control_server.py` wraps the *existing* orchestration cores rather than
re-implementing them: `init_fleet.build_fleet_config` / `write_fleet_config`,
`run_fleet.cmd_run` (stoppable + streamed), `hostadmin.create_swarm` /
`switch_challenge`. See `scripts/test_fleet_core.py` and `test_control_server.py`.
