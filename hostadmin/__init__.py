"""Implementation package for the root-level host-admin CLI (`setup.py`).

`setup.py` stays at the repo root as the thin dispatcher and back-compat
import surface — run `python setup.py <create|switch|sync|tacit|invite|
revoke|list>`, or `import setup` for programmatic use (run.py and
control_server.py do). Nothing should need to import `hostadmin.*` directly.

This package must live at the repo root: the swarm runs `python setup.py
sync` inside throwaway git worktrees, and a root-level tracked package is
present in every worktree automatically.

Modules:
  config_io.py         swarm.admin.json / .swarm-cache.json / fleet config
                       read+write, placeholder templating, atomic JSON writes
  prompting.py         interactive prompt helpers for the wizards
  http.py              shared POST-JSON helper + mainnet API GET
  challenges_bridge.py lazy loader for server/challenges.py (degrades cleanly
                       when server/ is absent from a trimmed clone)
  railway.py           Railway CLI wrappers + RailwayError
  tacit.py             tacit-knowledge wizard (also used by run.py / the
                       control-ui companion)
  contributors.py      invite / revoke / list host-admin commands
  swarm.py             create / switch / sync flows (Railway provisioning,
                       config push, pool seeding)
"""
