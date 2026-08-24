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

## Observability (Prometheus + Grafana)

Prometheus and Grafana are provisioned as code (`docker/grafana/provisioning/`,
`docker/grafana/dashboards/zeitgeist.json`) and come up with `docker compose
-f docker/docker-compose.yml up -d` alongside the rest of the stack. Both
bind to loopback only, matching the same "not on 0.0.0.0" policy as Neo4j.

- **URLs** (loopback only — see the SSH tunnel recipe below to reach them
  from your laptop):
  - Prometheus: `http://127.0.0.1:9090`
  - Grafana: `http://127.0.0.1:3000` (dashboard: "Zeitgeist Overview",
    `uid: zeitgeist-main`)
- **Grafana login**: user `admin`, password from `GRAFANA_PASSWORD` in
  `docker/.env` (falls back to `zeitgeist-dev` if unset — fine for local
  dev, set a real value on the VPS the same way `NEO4J_PASSWORD` is set).
  `docker/.env.example` documents the placeholder.
- **API's public port also serves metrics (controller ruling)**: the `api`
  service's port 8000 is published outside loopback (it's the public
  dashboard/API endpoint) and it also serves `/metrics` on that same port.
  This is a conscious acceptance, not an oversight: its contents are
  non-sensitive by design — graph counts are already public via `/stats`,
  and the rest is process internals (request counts, latencies) with no
  secrets or user data. Every other observability surface (Prometheus,
  Grafana, and every other service's own `/metrics`) stays loopback-only;
  `api` is the one deliberate exception.
- **SSH tunnel from your laptop to the VPS** (both services are loopback-only
  on the server, same reasoning as Neo4j/Kafka in the Provision section
  above):
  ```bash
  ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 user@host
  ```
  Then open `http://127.0.0.1:3000` (Grafana) and `http://127.0.0.1:9090`
  (Prometheus) locally as usual. Keep the SSH session open for the duration.

### Alerts

Three rules, all in Grafana-managed alerting (folder `zeitgeist`, group
`zeitgeist-alerts`), evaluated every 1m:

- **`Pipeline stalled (freshness)`** — `time() - zeitgeist_last_success_timestamp
  > 2700` for 5m (i.e. no successful ingestor run in ~45+ minutes).
  `noDataState: Alerting` is deliberate: if the ingestor's `/metrics`
  endpoint disappears entirely (container stopped/crashed — confirmed by
  live test below, this actually shows up as the *series vanishing*, not a
  climbing number, because Prometheus stale-marks a metric as soon as a
  scrape fails outright rather than waiting out its lookback window), that
  is itself staleness and must alert, not go silent.
  **First response**: `docker compose -f docker/docker-compose.yml ps
  ingestor` and `... logs --tail 100 ingestor`. Most likely causes: GDELT
  download failing repeatedly, container crash-looping, or the container
  stopped. Restart with `docker compose -f docker/docker-compose.yml start
  ingestor` (or `up -d ingestor` if it needs rebuilding); freshness should
  drop back to a small number within one poll cycle of the ingestor's next
  successful run.
- **`Kafka consumer lag high`** — `max(kafka_consumergroup_lag) > 10000`
  for 15m across any of the 5 consumer groups (`api-broadcast`, `extractor`,
  `graph-writer`, `llm-extractor`, `sampler`). `noDataState: OK` here (no
  lag data isn't itself a problem the way no freshness data is).
  **First response**: check which group via the "Consumer lag per group"
  panel or `curl -s http://127.0.0.1:9090/api/v1/query?query=kafka_consumergroup_lag`,
  then `docker compose -f docker/docker-compose.yml logs --tail 100
  <that-service>` — look for the consumer crash-looping, throwing on a bad
  message, or simply being slower than the produce rate (e.g. LLM tier
  backed up behind its daily budget cap). Restarting the stuck consumer is
  usually sufficient; Kafka retains the backlog.
  **Coverage gap, honestly stated**: this rule depends on `kafka-exporter`
  itself being alive to expose `kafka_consumergroup_lag` at all — if the
  exporter dies, the lag alert goes quiet rather than firing. It's also
  numerically blind to the two lowest-volume consumers, `llm-extractor` and
  `resolver`: their throughput is so low (gated by daily LLM/ER call
  budgets) that even a fully stuck consumer may never accumulate 10000
  unconsumed messages to cross the threshold. The new `Scrape target down`
  rule below closes both gaps — it fires on the exporter itself going dark,
  and on any consumer's process dying outright, independent of message
  volume.
- **`Scrape target down`** — `min(up) < 1` for 10m (i.e. at least one
  Prometheus scrape target — any of `api`, `extractor`, `graph-writer`,
  `ingestor`, `kafka-exporter`, `llm-extractor`, `resolver`, `sampler` — is
  unreachable). `noDataState: Alerting`, same reasoning as the freshness
  rule: an empty `up` result is itself a failure worth flagging, not
  something to stay silent on.
  **First response**: `curl -s http://127.0.0.1:9090/api/v1/targets` (or the
  Prometheus UI's Targets page) to see which target is down and why
  (`lastError` field — usually a DNS/connection failure meaning the
  container is stopped or crash-looping). `docker compose -f
  docker/docker-compose.yml ps <service>` and `... logs --tail 100
  <service>`, then `... start <service>` (or `up -d <service>`). This is
  the general-purpose backstop for exactly the kind of outage demonstrated
  live below — it would have caught the ingestor stall even without the
  freshness metric's own alert.
- **Alert delivery is not wired up yet**: all three rules route to Grafana's
  default, unconfigured contact point (`grafana-default-email`) — they will
  evaluate and show as firing in the Grafana UI/API, but no email or webhook
  is actually sent anywhere. Actual notification delivery (real SMTP
  credentials + recipient, or a webhook) is future work; until then, alert
  state must be checked by looking at the dashboard/UI rather than waiting
  for a page.

### Cost panel pricing caveat

The cost panel's PromQL hardcodes `claude-haiku` pricing ($1/$5 per M
input/output tokens) — update `docker/grafana/dashboards/zeitgeist.json`
panel 4 (the cost panel's query) if `LLM_MODEL` ever changes to a different
model tier. Also note: cached-token reads (`zeitgeist_llm_cached_tokens_total`,
priced by Anthropic at roughly 0.1x the input rate) are collected as a
counter but are not priced into the panel's formula — the panel slightly
underestimates actual spend whenever prompt caching is in effect.

### Counter-restart caveat

`zeitgeist_llm_input_tokens_total`, `_output_tokens_total`,
`zeitgeist_llm_dispositions_total`, and similar `_total` counters are
in-process (they reset to 0 whenever a service container restarts — e.g.
during a deploy). Panels built on `increase(...)` or `rate(...)` over a
window tolerate this correctly (a counter reset inside the window is
detected and handled by PromQL), but reading a raw `_total` value directly
as "spend so far" is misleading right after any restart — it reflects only
activity since that process started, not the full day. If a cost/token/
disposition panel reads 0 right after a deploy or restart, check `docker
compose ... ps <service>` uptime before assuming something is broken; it
may simply not have had a UTC-midnight-to-now window's worth of runtime yet
for its own counters, separate from the sampler/LLM daily budget files
(`llm_budget.json`, `sampler_budget.json`, `er_budget.json`) which persist
across restarts on their `*-state` volumes.

### Live-verified: freshness climbs on ingestor stop, recovers on restart

Confirmed on 2026-08-23 against the live stack (`time() -
zeitgeist_last_success_timestamp`, dashboard "Freshness" stat panel):

| step | time (UTC) | freshness query result | ingestor target health |
|---|---|---|---|
| baseline | 21:33:37 | (ingestor stopped at this instant) | — |
| sample 1 | 21:33:43 | `831.9s` (last good scrape before stop took effect) | up |
| sample 2 | 21:34:43 | **no data** | down — `dial tcp: lookup ingestor on 127.0.0.11:53: no such host` |
| sample 3 | 21:37:36 | **no data** | down (same DNS error) |
| sample 4 | 21:38:36 | **no data** | down (same DNS error) |
| sample 5 | 21:39:36 | **no data** | down (same DNS error) |
| restart | 21:39:45 | `docker compose start ingestor` issued | — |
| post-restart 1 | 21:40:01 | `11.9s` | up |
| post-restart 2 | 21:40:22 | `32.1s` | up |
| post-restart 3 | 21:40:42 | `52.4s` | up |

**What actually happens, precisely**: `docker compose stop` removes the
container from the Docker network's internal DNS, so the very next scrape
attempt fails with a DNS lookup error (not "connection refused"). Prometheus
stale-marks the series immediately on a failed scrape rather than serving
the last known value for its usual 5-minute lookback window — so on the
dashboard the "Freshness" stat panel does not visibly *climb*; it goes
blank/"No data" almost immediately (within one ~15s scrape interval) and
stays that way for as long as the container is stopped. This is exactly
why the freshness alert rule uses `noDataState: Alerting` — a vanished
metric is the real-world signature of a stalled pipeline here, not a
climbing number. On restart, the ingestor's gauge is reseeded (the value
observed above, `~12s`, is the seed/first-real-timestamp — see the LLM tier
note above for the general pattern of in-process metrics resetting on
restart) and then climbs normally with wall-clock time between polls, and
the target flips back to `up` within one scrape cycle.

## Known failure modes

- **GDELT missed window / download error**: ingestor logs the failure and retries next
  interval; no action needed unless persistent.
- **Kafka won't start after reboot**: `docker compose ... up -d` again; volume
  `kafka-data` preserves the log.
- **Restore from backup**: stop stack, `neo4j-admin database load` from the newest
  dump in `~/backups`, start stack.

## Continuous deployment

Pushes to `main` deploy automatically: the `deploy` job in `.github/workflows/ci.yml`
runs only after lint, unit, and integration tests pass, then rsyncs the repo to the
VPS (same privacy excludes as `scripts/deploy.sh`) and runs
`docker compose up -d --build` there. It authenticates with a dedicated deploy key
(`~/.ssh/zeitgeist_deploy` locally; revoke by removing its line from the server's
`/root/.ssh/authorized_keys`). Required GitHub repo secrets: `DEPLOY_SSH_KEY`
(the private key file contents) and `DEPLOY_HOST` (the server IP).
`scripts/deploy.sh` remains for manual/emergency deploys.

### CD gap: bind-mounted config files

`docker compose up -d` only recreates containers whose compose definition changed —
edits to bind-mounted config files (Caddyfile, prometheus.yml, grafana provisioning)
rsync to disk but are NOT reloaded automatically. After deploying such changes, run
`docker compose -f docker/docker-compose.yml restart caddy` (or prometheus/grafana).
Candidate future fix: add a reload step to the deploy workflow.
