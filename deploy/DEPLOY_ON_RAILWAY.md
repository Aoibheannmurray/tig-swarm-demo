# Deploy on Railway (browser-only host setup)

The **easiest** way to host a swarm: one click, no terminal, no Railway CLI.
This is the host-side counterpart to the contributor join link — together they
make "create a swarm → invite a friend" a fully browser-based flow (server-first
onboarding plan, §9 / P5).

It works because the coordination server **self-configures on first boot**:

- Swarm config (name, type, active challenge, thresholds) is read from
  environment variables (`server/db.py:_apply_env_swarm_config`).
- `admin_key` and the base `swarm_password` are generated on first boot when
  not supplied (or taken from `ADMIN_KEY` / `SWARM_PASSWORD` env).
- Initial algorithm code + the seed pool are populated from the snapshot baked
  into the image (`server/first_boot.py`) — so agents start from real code even
  with no host clone to run `setup.py create`.

## The button

Add this to a public README once the template is published (see below):

```markdown
[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template/<your-template-id>)
```

Railway "Deploy Template" is created once from the Railway dashboard, pointing
at this repo. Use the spec below when creating it.

## Template spec

One service, one volume:

| Setting | Value |
|---------|-------|
| Source | this repo (`Dockerfile` at root) |
| Volume | mount at `/data` (persistent SQLite lives here) |
| Healthcheck | `GET /health` |

### Environment variables

| Var | How | Purpose |
|-----|-----|---------|
| `DATA_DIR` | fixed: `/data` | SQLite on the persistent volume |
| `ADMIN_KEY` | template **generated secret** | owner/admin auth — surfaced in Railway's Variables tab |
| `SWARM_PASSWORD` | template **generated secret** | base contributor password |
| `SWARM_NAME` | user input | display name |
| `SWARM_TYPE` | user input (`cpu`/`gpu`, default `cpu`) | hardware class |
| `ACTIVE_CHALLENGE` | user input (default `satisfiability`) | starting challenge |
| `TRUSTED_PROXY` | fixed: `1` | Railway fronts the container |

No `SWARM_CHALLENGES_B64` is needed — per-challenge config falls back to the
built-in defaults, and the active challenge is tunable afterward from the
hosted Admin Console (`/admin/`), which replaces `setup.py switch` for
button-deployed swarms.

## After it deploys

1. Open Railway's **Variables** tab and copy `ADMIN_KEY` and `SWARM_PASSWORD`.
2. Visit `https://<your-app>.up.railway.app/admin/`, sign in with the admin key
   (and the base password to generate invites).
3. Create a **join link** for each contributor — they onboard entirely in the
   browser (`/join`), optionally running in the cloud via the hosted runner.

## Optional: hosted fleet runner (Tier 1)

To let contributors run **zero-install** (agents run on your infrastructure),
deploy the runner and point the swarm at it. This adds a "Run in the cloud"
tab to the join page.

**From a clone (one command):**
```bash
python setup.py create-runner
```
It deploys `runner/Dockerfile` as its own Railway service (generated
`RUNNER_SECRET_KEY`, your swarm URL + admin key wired in, a `/data` volume),
waits for it, and enables the tab.

**From the Railway dashboard (no clone):**
1. Add a service from `runner/Dockerfile` (set `RAILWAY_DOCKERFILE_PATH=runner/Dockerfile`).
2. Set its variables (detail in [../runner/README.md](../runner/README.md)):
   `RUNNER_SECRET_KEY` (`python -m runner.vault --generate`),
   `COORDINATION_SERVER_URL` = your swarm URL, `RUNNER_ADMIN_KEY` = the swarm's
   `ADMIN_KEY`; add a volume at `/data`.
3. Enable the tab: `POST <server>/api/admin/config?key=runner_url&value=<runner-url>`
   with body `{"admin_key": "<admin-key>"}`.
