#!/usr/bin/env bash
# Deploy zeitgeist to a VPS: ./scripts/deploy.sh user@host
set -euo pipefail

HOST="${1:?usage: deploy.sh user@host}"
REMOTE_DIR="~/zeitgeist"

rsync -az --delete \
  --exclude .git --exclude .venv --exclude .pytest_cache --exclude .ruff_cache \
  --exclude state --exclude data \
  ./ "$HOST:$REMOTE_DIR/"

ssh "$HOST" "cd $REMOTE_DIR && docker compose -f docker/docker-compose.yml up -d --build"
echo "deployed. dashboard: http://$(echo "$HOST" | cut -d@ -f2):8000/"
