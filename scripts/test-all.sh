#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  python3.12 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements-dev.txt

if [[ "$(uname -s)" == Linux ]]; then
  .venv/bin/python -m playwright install --with-deps chromium
else
  .venv/bin/python -m playwright install chromium
fi

.venv/bin/python -m pytest -q tests -p no:cacheprovider

# A unique Compose project keeps these volumes separate from the normal canvas.
export COMPOSE_PROJECT_NAME="canvascheck-$$"
trap 'docker compose down -v' EXIT
docker compose up --build -d
for attempt in {1..30}; do
  if .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/state', timeout=2)" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
.venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/state', timeout=2)"
RUN_DOCKER_INTEGRATION=1 .venv/bin/python -m pytest -q tests/integration -p no:cacheprovider
RUN_BROWSER_INTEGRATION=1 .venv/bin/python -m pytest -q tests/browser -p no:cacheprovider
