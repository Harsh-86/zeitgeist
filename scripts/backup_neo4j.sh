#!/usr/bin/env bash
# Nightly Neo4j dump. Install on the VPS cron: 0 3 * * * ~/zeitgeist/scripts/backup_neo4j.sh
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
STAMP="$(date +%Y%m%d)"
mkdir -p "$BACKUP_DIR"

cd "$(dirname "$0")/.."
docker compose -f docker/docker-compose.yml stop neo4j
docker compose -f docker/docker-compose.yml run --rm \
  -v "$BACKUP_DIR:/backups" neo4j \
  neo4j-admin database dump neo4j --to-path=/backups
mv "$BACKUP_DIR/neo4j.dump" "$BACKUP_DIR/neo4j-$STAMP.dump"
docker compose -f docker/docker-compose.yml start neo4j

ls -t "$BACKUP_DIR"/neo4j-*.dump | tail -n +8 | xargs -r rm  # keep last 7
echo "backup written: $BACKUP_DIR/neo4j-$STAMP.dump"
