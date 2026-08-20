# zeitgeist

A living, self-building temporal knowledge graph of world events.

Watches the GDELT news firehose in real time, streams it through Kafka,
and continuously grows a temporal knowledge graph in Neo4j — with a live
dashboard where you can watch the world become a graph.

## Quick start

```bash
make up      # start everything (Kafka, Neo4j, services, dashboard)
make test    # unit tests
make smoke   # end-to-end smoke test
```

Operations: see [RUNBOOK.md](RUNBOOK.md)
