# CLAUDE.md — server/ (coordination server)

The per-swarm coordination server: stores scores/config, serves the leaderboard
and dashboard, and pushes live updates over WebSocket. **One independent
deployment per swarm** — no multi-tenancy.

## Stack & run

- FastAPI + uvicorn + aiosqlite (SQLite). Deps in `server/requirements.txt`
  (separate from the repo-root `requirements.txt`).
- Run locally: `pip install -r requirements.txt && uvicorn server:app`
  (defaults to port 8080). `DATA_DIR` sets where `swarm.db` lives — defaults to
  this dir; in prod it's a Railway volume (see `entrypoint.sh`).
- **Self-contained.** The production `Dockerfile` at the repo root copies *only*
  `server/` (plus the built dashboard). Don't add imports reaching into
  `scripts/` or the repo root — they won't exist in the image.

## Layout

- `server.py` — FastAPI app + routes; `app` is the ASGI entry point.
- `db.py` — SQLite schema/access; config applied from env on first boot.
- `models.py` / `api_models.py` — internal and API data shapes.
- `challenges.py`, `tiers.py`, `dedup.py`, `names.py`, `ws_events.py` — domain
  logic. `entrypoint.sh` — container start.
- `data/swarm.db` — local dev DB (gitignored).

## Tests

No pytest — `test_*.py` are self-running scripts (`if __name__ == "__main__":
asyncio.run(...)`). Each sets `DATA_DIR` to a temp dir *before* importing server
modules, so it runs hermetically. Run one directly, e.g.
`python server/test_infeasible_floor_trap.py`.
