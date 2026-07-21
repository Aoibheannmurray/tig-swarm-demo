# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Report them
privately via GitHub: **Security tab → Report a vulnerability** on this
repository. This project is provided as-is; reports are reviewed on a
best-effort basis with no response-time commitment.

## Scope notes for self-hosters

- The coordination server is designed to be reachable by your contributors;
  admin endpoints require the admin key from `swarm.admin.json`. Keep that
  file private — it is gitignored and must never be committed.
- Dashboard scores, activity, and leaderboard data are public. Solver source is
  contributor-only: `/api/state` returns its code-bearing fields as `null`
  unless valid `X-Username` and `X-Swarm-Password` headers are supplied, and
  experiment-history endpoints reject `include_code=true` without those
  headers. Revoked contributor credentials cannot retrieve code.
- The local companion server (`run.py --ui` / `control_server.py`) binds to
  localhost and is meant for a single machine; don't expose it publicly.
- Secrets (provider API keys, `C3_API_KEY`) live in environment variables or
  your local `fleet.config.json`, which is gitignored. Never commit keys.
- Swarm agents execute LLM-generated Rust inside Docker (or on C3). Treat the
  benchmark containers as untrusted-code sandboxes: keep Docker up to date and
  don't mount extra host paths into them.
