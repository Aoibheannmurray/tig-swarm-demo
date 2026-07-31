#!/bin/sh
# Container entrypoint. Ensures the data dir exists, then execs uvicorn.
# Persistence is provided by the Railway volume mounted at $DATA_DIR.
set -e

DB_DIR="${DATA_DIR:-/app}"
PORT="${PORT:-8080}"

# Railway always fronts the container with its reverse proxy, so the first
# X-Forwarded-For hop is trustworthy here. server.py only honors XFF for the
# per-IP auth throttle when this is set; bare-metal/dev runs without a proxy
# must leave it unset so a client can't spoof its throttle bucket.
export TRUSTED_PROXY="${TRUSTED_PROXY:-1}"

mkdir -p "$DB_DIR"
exec uvicorn server:app --host 0.0.0.0 --port "$PORT"
