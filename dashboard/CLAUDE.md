# CLAUDE.md — dashboard/ (web UI)

The swarm's web UI: leaderboard, benchmark charts, and idea / diversity /
trajectory views. Plain TypeScript + d3, built by Vite. No framework.

## Stack & run

- Vite + TypeScript + d3 (`vitest` for tests). Deps in `package.json`.
- Dev: `npm install && npm run dev`. Build: `npm run build` (`tsc && vite build`
  → `dist/`). Test: `npm test` (e.g. `src/lib/format.test.ts`).
- Multi-page: each top-level `*.html` is a separate Vite entry, wired in
  `vite.config.ts` (`rollupOptions.input`: `index`, `ideas`, `diversity`,
  `benchmark`, `trajectories`, `leaderboard`).

## src/ layout

- Page entries: `main.ts` is the home page; every other page has its own entry
  at `src/pages/<name>/main.ts` (`ideas`, `diversity`, `benchmark`,
  `trajectories`, `leaderboard`). Each `*.html` loads its page's `main.ts`.
- `lib/` — shared helpers every page uses (`bootstrap`, `websocket`, `format`,
  `colors`, `replay`, `swarmConfig`, …).
- `panels/` — the UI panels (`chart`, `leaderboard`, `feed`, `stats`,
  `diversity`, `challenge-selector`).
- `challenges/` — per-challenge view logic, one file each, dispatched via
  `registry.ts`.
- `types.ts` — shared types; `style.css`.

## How it ships

The production `Dockerfile` (repo root) builds this (`npm run build`) and copies
`dist/` into the server image as `static/`, which the FastAPI server serves. So
a dashboard change reaches users via a **server** redeploy, not a separate
deploy.
