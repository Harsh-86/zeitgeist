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

## Plug the news into Claude (MCP)

The graph doubles as an MCP server: LLM-free, read-only tools (entity search,
timelines, connections, recent events, stats, validated Cypher) that your own
Claude reasons over — no API key needed on this side. It talks bolt to Neo4j,
so run the local stack first (`make up`) or tunnel to a server
(`ssh -L 7687:127.0.0.1:7687 <host>`).

Claude Code, from the repo directory:

```bash
claude mcp add zeitgeist -- uv run python -m zeitgeist.mcp_server
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "zeitgeist": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/Side-Project", "python", "-m", "zeitgeist.mcp_server"]
    }
  }
}
```

Connection env vars (defaults fit the local stack): `NEO4J_URI`
(`bolt://localhost:7687`), `NEO4J_USER` (`neo4j`), `NEO4J_PASSWORD`
(`zeitgeist-dev`).

Operations: see [RUNBOOK.md](RUNBOOK.md)
