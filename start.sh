#!/usr/bin/env bash
# Start the CTS Scoreboard in production mode using gunicorn with gevent.
#
# Usage:
#   ./start.sh              # defaults: 0.0.0.0:5000, 1 worker
#   ./start.sh --bind 0.0.0.0:8080
#   ./start.sh --workers 2
#
# Any extra arguments are forwarded to gunicorn.

set -euo pipefail
cd "$(dirname "$0")"

# Activate the project venv if present
if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

exec gunicorn \
    --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
    --bind "${BIND:-0.0.0.0:5000}" \
    --workers "${WORKERS:-1}" \
    --access-logfile - \
    --error-logfile - \
    "$@" \
    CTS_Scoreboard:app
