# zeitgeist runbook

## Provision (once, manual)

1. Hetzner Cloud → create server: CX32 (4 vCPU / 8 GB — Kafka + Neo4j need the RAM),
   Ubuntu 24.04, add your SSH key. (~€15/mo, inside budget.)
2. SSH in, then:
   ```bash
   apt update && apt install -y docker.io docker-compose-v2 rsync
   ufw allow OpenSSH && ufw allow 8000/tcp && ufw enable
   ```
   **Important**: UFW alone does NOT protect Neo4j (7474/7687) or Kafka (9092/29092).
   Docker manages its own iptables rules for published ports, and those rules are
   inserted ahead of UFW's chain — so a container port published as `0.0.0.0:PORT`
   is reachable from the internet even while UFW reports it closed. The real
   protection here is that `docker/docker-compose.yml` binds those ports to
   `127.0.0.1` (loopback only), not to UFW. Do not "fix" this by publishing them on
   `0.0.0.0` and relying on UFW to block them — it won't.
3. Set a strong Neo4j password before starting the stack (the compose file falls
   back to the `zeitgeist-dev` default otherwise, which is fine for local dev but
   must not be used on the VPS). Set it via one of:
   - `/etc/environment` on the server: add `NEO4J_PASSWORD=<strong-password>`, then
     re-login (or `source /etc/environment`) so `docker compose` picks it up, or
   - an `.env` file next to `docker/docker-compose.yml` (i.e. `docker/.env`) with
     `NEO4J_PASSWORD=<strong-password>` — docker compose loads this automatically.
   Note: the Neo4j healthcheck in the compose file uses the literal `zeitgeist-dev`
   password (env substitution inside healthchecks is unreliable across compose
   versions), so if you set a custom `NEO4J_PASSWORD`, the `neo4j` container will
   report itself unhealthy even though it's running fine with the real password —
   this is a known, accepted tradeoff.
4. From your laptop: `./scripts/deploy.sh root@<server-ip>`
5. Install the backup cron on the server:
   `crontab -e` → `0 3 * * * ~/zeitgeist/scripts/backup_neo4j.sh >> ~/backup.log 2>&1`

## Deploy an update

`./scripts/deploy.sh root@<server-ip>` — rsyncs and rebuilds; Kafka and Neo4j
volumes persist across deploys.

## Health checks

- `curl http://<ip>:8000/healthz` → `{"status":"ok"}`
- `curl http://<ip>:8000/stats` → entity/event counts (should grow every ~15 min)
- `ssh root@<ip> 'cd zeitgeist && docker compose -f docker/docker-compose.yml logs --tail 50 ingestor'`

## Known failure modes

- **GDELT missed window / download error**: ingestor logs the failure and retries next
  interval; no action needed unless persistent.
- **Kafka won't start after reboot**: `docker compose ... up -d` again; volume
  `kafka-data` preserves the log.
- **Restore from backup**: stop stack, `neo4j-admin database load` from the newest
  dump in `~/backups`, start stack.
