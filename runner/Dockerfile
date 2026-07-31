# Hosted fleet runner — its own Railway service (NOT the coordination server).
# Unlike the coordination server's image (server/ only), this needs the FULL
# repo: it spawns `python run.py` per contributor, which drives scripts/ +
# setup.py in per-contributor git clones. It also needs git (worktrees /
# --local clones) and the c3 CLI (cloud benchmark submission).
FROM python:3.12-slim

# git: per-contributor workspace clones + run_fleet's worktrees.
# curl + ca-certificates: install the c3 CLI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# c3 CLI — hosted agents benchmark on C3 (compute is validated C3-only at
# enrollment). Pin/vendor this once the redistribution story is settled
# (plan §14 open question); install.sh at build time for now.
RUN curl -fsSL https://cthree.cloud/install.sh | sh || \
    echo "WARNING: c3 CLI install failed at build; hosted benchmarks will fail until fixed"

WORKDIR /app
# Runner service deps + the worker deps the spawned fleets import.
COPY runner/requirements.txt runner/requirements.txt
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r runner/requirements.txt -r requirements.txt

# The whole repo (run.py, scripts/, setup.py, hostadmin/, src/, …).
COPY . .

# Make /app a git repo so the supervisor can `git clone --local` it into an
# isolated per-contributor workspace (the image ships without .git — both
# .dockerignore and .ignore drop it). run_fleet needs each workspace to be a
# git repo for its per-agent worktrees.
RUN git init -q \
 && git -c user.email=runner@tig.local -c user.name=tig-runner add -A \
 && git -c user.email=runner@tig.local -c user.name=tig-runner commit -qm "runner base image" \
 && git config --global --add safe.directory '*'

# RUNNER_DATA_DIR (enrollment DB) and RUNNER_WORKSPACES (per-contributor
# clones) should be mounted on a persistent Railway volume in production.
ENV RUNNER_DATA_DIR=/data \
    RUNNER_WORKSPACES=/data/workspaces \
    RUNNER_REPO_ROOT=/app

EXPOSE 8080
CMD ["sh", "-c", "uvicorn runner.service:app --host 0.0.0.0 --port ${PORT:-8080}"]
