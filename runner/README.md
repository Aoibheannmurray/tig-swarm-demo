# runner/ — hosted fleet runner (Tier 1)

A standalone service that runs contributor fleets in the cloud, so a
contributor can join a swarm with **zero local install**: keys are enrolled
via the runner's API and agents run here. (No web UI drives it yet — the
`/api/runner/*` surface below is the way in.)

It is a **separate deployment** from the coordination server (`server/`): its
own Railway service, image, and volume. The coordination server stays
self-contained and never holds raw LLM keys.

## How it fits

```
contributor browser ──► coordination server (/api/contributor/*)   # plan authored here
        │                          ▲
        │ paste keys               │ auth + plan fetch (server-to-server)
        ▼                          │
   runner service ────────────────┘
     • Fernet-encrypted key vault (own SQLite volume)
     • one fleet per contributor, isolated git clone, `python run.py`
     • C3-only compute (no Docker; LLM code never executed on the box)
```

## Endpoints (`/api/runner/*`)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/enroll` | contributor creds | store keys, validate plan, launch fleet |
| GET | `/status` | contributor creds | enrollment + fleet state (keys masked) |
| GET | `/logs` | contributor creds | recent fleet log lines |
| DELETE | `/enroll` | contributor creds | stop fleet, purge keys |
| POST | `/admin/revoke` | `X-Admin-Key` | host teardown (wired from `setup.py revoke`) |
| GET | `/health` | none | readiness + capacity |

Contributor auth is delegated to the coordination server's
`GET /api/contributor/me` — the runner holds no swarm password. Keys are
Fernet-encrypted with `RUNNER_SECRET_KEY` before they touch disk, decrypted
only into the fleet's workspace secrets file, never into the runner's own env.

## Configuration (env)

| Var | Required | Meaning |
|-----|----------|---------|
| `RUNNER_SECRET_KEY` | yes | Fernet key for the vault. Generate: `python -m runner.vault --generate` |
| `COORDINATION_SERVER_URL` | yes | the swarm server the runner authenticates against |
| `RUNNER_ADMIN_KEY` | yes | shared secret for `/admin/revoke` — set to the swarm's `admin_key` |
| `RUNNER_DATA_DIR` | prod | enrollment DB location (persistent volume) |
| `RUNNER_WORKSPACES` | prod | per-contributor clones (persistent volume) |
| `RUNNER_REPO_ROOT` | | repo to clone per fleet (default: this checkout) |
| `RUNNER_MAX_AGENTS_PER_CONTRIBUTOR` | | default 8 |
| `RUNNER_MAX_TOTAL_AGENTS` | | default 64 |

To let `setup.py revoke` tear down hosted fleets, add `"runner_url"` to
`swarm.admin.json` on the host.

## Deploy on Railway (recommended)

From a clone of an existing swarm, one command deploys this service and points
the swarm at it:

```bash
python setup.py create-runner
```

It provisions a Railway service built from this `Dockerfile` (via
`RAILWAY_DOCKERFILE_PATH`), generates `RUNNER_SECRET_KEY`, wires in the swarm's
URL + admin key, attaches a `/data` volume, waits for `/api/runner/health`, and
runs `set-runner` to record the runner's URL in the swarm config. Re-run to
update.

## Run locally

```bash
export RUNNER_SECRET_KEY=$(python -m runner.vault --generate)
export COORDINATION_SERVER_URL=https://your-swarm.up.railway.app
export RUNNER_ADMIN_KEY=<the swarm admin_key>
pip install -r runner/requirements.txt -r requirements.txt
uvicorn runner.service:app --port 8095
```

## Tests

No pytest — `test_*.py` are self-running (`python runner/test_runner.py`,
`python runner/test_service.py`). They point `RUNNER_SECRET_KEY` /
`RUNNER_DATA_DIR` at temp values and drive the supervisor with a fake
launcher, so nothing clones a repo or spawns a fleet.

## Security model

Hosted fleets are validated **C3-only** and **non-agentic** at enrollment, so
LLM-authored code is *submitted to C3*, never executed on the runner — which is
why per-contributor OS sandboxing isn't required. Keys are
encrypted at rest, shown only masked, injected only into a fleet's workspace
secrets file, and purged on unenroll/revoke. Contributors should still use
spend-limited keys (OpenRouter supports per-key caps) — the console says so.
