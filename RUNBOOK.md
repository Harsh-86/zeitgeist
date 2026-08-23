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

## LLM tier

The `llm-extractor` service enriches a sampled subset of events with Claude
(Haiku by default) instead of the rules-only extractor. It needs an Anthropic
API key.

- **Env file**: create `docker/.env` (copy `docker/.env.example`) with:
  ```
  ANTHROPIC_API_KEY=sk-ant-...
  LLM_MAX_CALLS_PER_DAY=200
  ```
  `docker/.env` is gitignored and auto-loaded by `docker compose -f
  docker/docker-compose.yml` for `${...}` interpolation (the compose file's
  directory is the project directory). Never commit it.
  **`docker/.env` must be created by hand on any new server** — `deploy.sh`
  deliberately never syncs it (it holds the real API key). Without it, the
  `llm-extractor` container exits immediately (missing `ANTHROPIC_API_KEY`)
  and restart-loops.
- **Measured cost / recommended cap**: at the measured $0.0029/call, 200
  calls/day is roughly $17-18/month and 400 calls/day is roughly $35/month.
  `docker/.env.example` defaults `LLM_MAX_CALLS_PER_DAY` to 200 — raise it
  deliberately if you want more LLM-tier coverage and are OK with the cost.
- **Spend-limit advice**: set a monthly spend limit on the API key/workspace
  at console.anthropic.com *in addition to* `LLM_MAX_CALLS_PER_DAY` — the app-level
  cap is defense in depth, not a substitute for the platform-enforced limit.
- **Two daily caps**: the `sampler` service admits at most
  `SAMPLER_MIN_SCORE`-qualifying events into `llm.queue` per day (its own
  budget file, `sampler_budget.json`), and `llm-extractor` independently caps
  itself at `LLM_MAX_CALLS_PER_DAY` calls/day (`llm_budget.json`). Both reset
  at UTC midnight and persist across restarts via their `*-state` volumes.
  Once either cap is hit for the day, further over-cap messages are dropped
  with a log line the same day — rules-tier coverage of those events is
  unaffected. The `llm.queue` backlog does **not** carry over and get
  reprocessed the next day; it is simply not consumed until the cap resets.
- **Verify it's live**: `docker compose -f docker/docker-compose.yml logs -f
  llm-extractor` should show `llm call: in=... out=... cached=...` lines, and
  `MATCH (ev:Event {tier:'llm'}) RETURN count(ev)` in cypher-shell should grow.

## Entity resolution (ER / resolver)

The `resolver` service periodically (every `RESOLVER_INTERVAL_SECONDS`,
default 3600s) scans the graph and does two things with Claude, writing only
additive metadata/edges — it never merges, deletes, or rewrites existing
nodes or edges:

1. **Generic screening**: every never-screened `Entity` with at least
   `ER_MIN_EVENTS` (default 3) `ACTOR1_IN` events is sent to the judge, which
   decides whether the name is a generic role/description (e.g. `POLICE`,
   `GOVERNMENT`) rather than a specific named entity. The verdict is written
   as `is_generic` / `generic_confidence` / `generic_checked` properties on
   the `Entity` node — no edges are touched by this step.
2. **Pair judging**: candidate same-entity pairs (drawn from screened,
   non-generic, not-yet-aliased entities) are judged for whether they refer
   to the same real-world entity (e.g. abbreviation/variant spellings). Every
   judged pair gets an immutable `ER_JUDGED` edge recording the verdict; a
   `SAME` verdict at or above `ER_MIN_CONFIDENCE` (default 0.8) additionally
   writes an `ALIAS_OF` edge from the alias to the canonical entity.

It reuses the same `ANTHROPIC_API_KEY` as the LLM tier and shares
`docker/.env` — no extra secret to provision. It only depends on `neo4j`
being healthy (it never touches Kafka).

- **Env file**: `docker/.env` additionally needs (or accepts the default):
  ```
  ER_MAX_CALLS_PER_DAY=100
  ```
  `docker/.env.example` defaults this to 100.
- **Measured cost**: ER calls run ~$0.0003 each (short prompts — a handful of
  sample relations per entity), so 100 calls/day is roughly $0.03/day
  (~$1/month) — negligible next to the LLM-extractor tier.
- **Budget**: `ER_MAX_CALLS_PER_DAY` caps combined screening + judging calls
  per day, persisted in `er_budget.json` on the `resolver-state` volume,
  independent of the sampler/llm-extractor budgets. The cycle stops the
  instant the budget is exhausted (no partial reads past that point) and
  picks up where it left off on the next `RESOLVER_INTERVAL_SECONDS` tick
  once the daily counter resets at UTC midnight.
- **Verify it's live**: `docker compose -f docker/docker-compose.yml logs -f
  resolver` should show `resolver cycle: screened=N judged=N aliased=N
  skipped=N budget_left=...` lines.
- **Reading canonical names**: downstream readers should resolve any
  `Entity` to its canonical form with:
  ```cypher
  MATCH (e:Entity)
  OPTIONAL MATCH (e)-[:ALIAS_OF]->(c)
  RETURN coalesce(c, e) AS canonical
  ```
  (single-hop is guaranteed — `ALIAS_OF` edges never chain).
- **Verifying no alias chains exist**: `write_alias` enforces single-hop by
  construction (a bidirectional guard refuses the write if either end
  already has an `ALIAS_OF` edge), but it's cheap to audit directly:
  ```cypher
  MATCH (a)-[:ALIAS_OF]->(b)-[:ALIAS_OF]->(c) RETURN a.name, b.name, c.name
  ```
  Expect zero rows, always.
- **Reversibility — undo a bad alias**: every write this service makes is a
  single edge or a few properties, so any mistake is a one-line Cypher fix
  in `cypher-shell` (no restart needed):
  ```cypher
  // Don't know the canonical name, only that "X" looks wrongly aliased?
  // Find what it currently resolves to first:
  MATCH (a:Entity {name: "X"})-[:ALIAS_OF]->(c) RETURN c.name

  // Delete a specific bad ALIAS_OF edge (does not touch either node):
  MATCH (a:Entity {name: "ALIAS_NAME"})-[r:ALIAS_OF]->(c:Entity {name: "CANONICAL_NAME"})
  DELETE r

  // Flip a wrong is_generic verdict (also clears generic_checked so it gets
  // re-screened next cycle, or set generic_checked = true to leave it fixed):
  MATCH (e:Entity {name: "ENTITY_NAME"})
  SET e.is_generic = false, e.generic_checked = null, e.generic_confidence = null

  // To force a pair to be re-judged, delete its ER_JUDGED edge too (otherwise
  // fetch_judged_pairs will keep skipping it):
  MATCH (a:Entity {name: "A"})-[j:ER_JUDGED]->(b:Entity {name: "B"})
  DELETE j
  ```
  Deleting an `ALIAS_OF` edge does not delete the `ER_JUDGED` edge that
  produced it (or vice versa) — delete both if you want the pair judged
  fresh. To stop the resolver entirely without losing its budget/state:
  `docker compose -f docker/docker-compose.yml stop resolver`.
- **Clearing a screening backlog faster**: generic screening runs before pair
  judging every cycle and only stops once the daily budget is exhausted, so a
  large backlog of never-screened entities can starve pair judging (and thus
  `ALIAS_OF` edges) for a long time — screening is one-time per entity (it
  never re-screens anything with `generic_checked` set), so a *temporary*
  bump is safe and doesn't waste spend. Two documented levers, either used
  temporarily then reverted:
  - Raise `ER_MAX_CALLS_PER_DAY` in `docker/.env` for a few days to burn down
    the backlog faster, then drop it back to the normal cap.
  - Raise `ER_MIN_EVENTS` to shrink the universe of entities eligible for
    screening in the first place (fewer, higher-signal entities per cycle).
  Current operator decision: the cap stays at 100/day and the backlog is
  left to drain over its natural ~3-week timeline — no bump applied.

## Known failure modes

- **GDELT missed window / download error**: ingestor logs the failure and retries next
  interval; no action needed unless persistent.
- **Kafka won't start after reboot**: `docker compose ... up -d` again; volume
  `kafka-data` preserves the log.
- **Restore from backup**: stop stack, `neo4j-admin database load` from the newest
  dump in `~/backups`, start stack.
