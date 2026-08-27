"""API service: health, stats, and live claim fan-out over websockets."""

import asyncio
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from zeitgeist.agent.query import validate_cypher
from zeitgeist.budget import DailyBudget
from zeitgeist.config import CLAIMS_TOPIC, Settings
from zeitgeist.kafka_utils import make_consumer
from zeitgeist.metrics import get_counter, get_gauge, start_metrics_server

logger = logging.getLogger("zeitgeist.api")

GRAPH_ENTITIES = get_gauge("zeitgeist_graph_entities", "Entities currently in the graph")
GRAPH_EVENTS = get_gauge("zeitgeist_graph_events", "Events currently in the graph")

ASK_QUESTIONS = get_counter(
    "zeitgeist_ask_questions_total", "Authorized /ask questions attempted"
)
ASK_DENIED = get_counter(
    "zeitgeist_ask_denied_total", "/ask attempts denied by the daily budget"
)
ASK_FAILED = get_counter(
    "zeitgeist_ask_failed_total", "/ask attempts that ended in an error body"
)

_MAX_QUESTION_CHARS = 500
_MAX_BODY_BYTES = 10_000

RECENT_CLAIMS_MATCH = "MATCH (s:Entity)-[:ACTOR1_IN]->(ev:Event)-[:ACTOR2]->(o:Entity) "
RECENT_CLAIMS_RETURN = (
    "RETURN s.name AS subject, ev.relation AS relation, o.name AS object "
    "ORDER BY ev.observed_at DESC LIMIT $limit"
)
RECENT_SINCE_CONDITION = "ev.observed_at >= datetime($since)"
RECENT_UNTIL_CONDITION = "ev.observed_at <= datetime($until)"
RECENT_TIER_CONDITION = "ev.tier = $tier"
# Tier-filtered requests (the frontend wire) also want the llm-only fields.
RECENT_TIER_RETURN = (
    "RETURN s.name AS subject, ev.relation AS relation, o.name AS object, "
    "ev.detail AS detail, ev.tier AS tier "
    "ORDER BY ev.observed_at DESC LIMIT $limit"
)
RECENT_CLAIMS_QUERY = RECENT_CLAIMS_MATCH + RECENT_CLAIMS_RETURN
_VALID_TIERS = {"rules", "llm"}

_DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def _frontend_dist() -> Path:
    """Resolve the built frontend directory, overridable via FRONTEND_DIST_PATH.

    In the Docker image the package is pip-installed into site-packages, so the
    source-tree-relative default (parents[3]/frontend/dist) does not resolve to
    /app/frontend/dist. Compose sets FRONTEND_DIST_PATH=/app/frontend/dist there.
    """
    override = os.getenv("FRONTEND_DIST_PATH")
    return Path(override) if override else _DEFAULT_FRONTEND_DIST


def _parse_window_param(name: str, value: str | None) -> str | None:
    """Normalize an ISO-8601 /recent window param; invalid values are ignored with a warning.

    Naive values (no offset) are interpreted as UTC by Neo4j's datetime() —
    the server default — which matches observed_at, always written as UTC.
    """
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        logger.warning("ignoring invalid %s value for /recent: %r", name, value[:100])
        return None


def _recent_claims_query(since: str | None, until: str | None, tier: str | None = None) -> str:
    """Assemble the /recent query; no params yields RECENT_CLAIMS_QUERY verbatim.

    A tier filter also switches to the wider RETURN (detail + tier columns),
    which the frontend wire ticker consumes.
    """
    conditions = []
    if since is not None:
        conditions.append(RECENT_SINCE_CONDITION)
    if until is not None:
        conditions.append(RECENT_UNTIL_CONDITION)
    if tier is not None:
        conditions.append(RECENT_TIER_CONDITION)
    where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
    returns = RECENT_TIER_RETURN if tier is not None else RECENT_CLAIMS_RETURN
    return RECENT_CLAIMS_MATCH + where + returns


def _ask_error(message: str) -> dict:
    """The /ask error body: every field present, error set — the shape never varies."""
    return {
        "answer": None,
        "cypher": None,
        "citations": [],
        "records_count": 0,
        "error": message,
    }


def _citations(records: list[dict]) -> list[str]:
    """Deduped, order-preserving truthy source_url values found in the records.

    Matches the bare key and dotted projections like "ev.source_url" (an
    unaliased RETURN ev.source_url), so citations survive either spelling.
    """
    urls: list[str] = []
    for record in records:
        for key, value in record.items():
            is_citation = key == "source_url" or key.endswith(".source_url")
            if is_citation and value and value not in urls:
                urls.append(value)
    return urls


class Broadcaster:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    def register(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    def unregister(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, text: str) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_text(text)
            except Exception:
                logger.exception("dropping unresponsive websocket client")
                self.unregister(ws)


def _consume_forever(broadcaster: Broadcaster, loop: asyncio.AbstractEventLoop) -> None:
    settings = Settings.from_env()
    consumer = make_consumer(settings.kafka_bootstrap, CLAIMS_TOPIC, group_id="api-broadcast")
    logger.info("api broadcasting %s", CLAIMS_TOPIC)
    while True:
        message = consumer.poll(1.0)
        if message is None or message.error():
            continue
        text = message.value().decode()
        asyncio.run_coroutine_threadsafe(broadcaster.broadcast(text), loop)


def create_app(
    driver=None, start_consumer: bool = False, agent=None, ask_budget=None
) -> FastAPI:
    broadcaster = Broadcaster()
    settings = Settings.from_env()

    if driver is None:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )

    if agent is None and settings.anthropic_api_key:
        import anthropic

        from zeitgeist.agent.query import QueryAgent

        agent = QueryAgent(
            anthropic.Anthropic(api_key=settings.anthropic_api_key), settings.llm_model
        )
        if ask_budget is None:
            ask_budget = DailyBudget(
                Path(settings.ask_budget_state_path),
                settings.ask_max_calls_per_day,
                now=datetime.now,
            )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_consumer:
            loop = asyncio.get_running_loop()
            thread = threading.Thread(
                target=_consume_forever, args=(broadcaster, loop), daemon=True
            )
            thread.start()
        yield

    app = FastAPI(title="zeitgeist", lifespan=lifespan)
    app.state.broadcaster = broadcaster
    app.state.driver = driver

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/stats")
    def stats() -> dict:
        with app.state.driver.session() as session:
            entities = session.run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
            events = session.run("MATCH (ev:Event) RETURN count(ev) AS n").single()["n"]
        return {"entities": entities, "events": events}

    @app.get("/recent")
    def recent(
        limit: int = 500,
        until: str | None = None,
        since: str | None = None,
        tier: str | None = None,
    ) -> dict:
        capped_limit = max(1, min(limit, 1000))
        params: dict = {"limit": capped_limit}
        normalized_since = _parse_window_param("since", since)
        normalized_until = _parse_window_param("until", until)
        valid_tier = tier if tier in _VALID_TIERS else None
        if tier is not None and valid_tier is None:
            logger.warning("ignoring invalid tier value for /recent: %r", tier[:100])
        if normalized_since is not None:
            params["since"] = normalized_since
        if normalized_until is not None:
            params["until"] = normalized_until
        if valid_tier is not None:
            params["tier"] = valid_tier
        query = _recent_claims_query(normalized_since, normalized_until, valid_tier)
        base_keys = ("subject", "relation", "object")
        keys = (*base_keys, "detail", "tier") if valid_tier is not None else base_keys
        try:
            with app.state.driver.session() as session:
                result = session.run(query, **params)
                claims = [{key: r[key] for key in keys} for r in result]
        except Exception:
            logger.warning("failed to fetch recent claims for /recent", exc_info=True)
            return {"claims": []}
        return {"claims": claims}

    def _run_cypher(cypher: str) -> list[dict]:
        with app.state.driver.session() as session:
            return session.execute_read(lambda tx: [dict(r) for r in tx.run(cypher)])

    def _spend_allowed() -> bool:
        return ask_budget is None or ask_budget.try_spend()

    def _answer_question(question: str) -> dict:
        if agent is None:
            return _ask_error("ask agent not configured")

        if not _spend_allowed():
            ASK_DENIED.inc()
            return _ask_error("daily ask budget exhausted")
        raw, _usage = agent.generate_cypher(question)
        cypher = validate_cypher(raw) if raw is not None else None
        if cypher is None:
            return _ask_error("could not generate a safe query")

        try:
            records = _run_cypher(cypher)
        except Exception as exc:
            logger.warning("/ask cypher failed, retrying once (cypher=%s)", cypher, exc_info=True)
            if not _spend_allowed():
                ASK_DENIED.inc()
                return _ask_error("daily ask budget exhausted")
            raw, _usage = agent.generate_cypher(question, error_feedback=f"{cypher}\n{exc}")
            cypher = validate_cypher(raw) if raw is not None else None
            if cypher is None:
                return _ask_error("could not generate a safe query")
            try:
                records = _run_cypher(cypher)
            except Exception:
                logger.warning("/ask cypher retry failed (cypher=%s)", cypher, exc_info=True)
                return _ask_error("query execution failed")

        if not _spend_allowed():
            ASK_DENIED.inc()
            return _ask_error("daily ask budget exhausted")
        answer, _usage = agent.synthesize(question, records)
        if answer is None:
            return _ask_error("could not synthesize an answer")

        return {
            "answer": answer,
            "cypher": cypher,
            "citations": _citations(records),
            "records_count": len(records),
            "error": None,
        }

    def _ask_flow(question: str) -> dict:
        try:
            return _answer_question(question)
        except Exception:
            logger.warning("unexpected failure answering /ask", exc_info=True)
            return _ask_error("internal error")

    @app.post("/ask")
    async def ask(request: Request) -> JSONResponse:
        if not settings.ask_token:
            return JSONResponse({"error": "ask endpoint disabled"}, status_code=403)
        provided = request.headers.get("X-Ask-Token", "")
        # Compare as bytes: compare_digest raises TypeError on non-ASCII str
        # input, and the header value is attacker-controlled.
        if not secrets.compare_digest(provided.encode(), settings.ask_token.encode()):
            return JSONResponse({"error": "invalid token"}, status_code=403)

        if int(request.headers.get("content-length") or 0) > _MAX_BODY_BYTES:
            return JSONResponse({"error": "request body too large"}, status_code=400)
        try:
            body = await request.json()
        except ValueError:
            body = None
        question = body.get("question") if isinstance(body, dict) else None
        if not isinstance(question, str) or not question.strip():
            return JSONResponse({"error": "question is required"}, status_code=400)
        if len(question) > _MAX_QUESTION_CHARS:
            return JSONResponse(
                {"error": f"question too long (max {_MAX_QUESTION_CHARS} characters)"},
                status_code=400,
            )

        ASK_QUESTIONS.inc()
        result = await run_in_threadpool(_ask_flow, question)
        if result["error"] is not None:
            ASK_FAILED.inc()
        return JSONResponse(result)

    @app.get("/metrics")
    def metrics() -> Response:
        try:
            with app.state.driver.session() as session:
                entities = session.run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
                events = session.run("MATCH (ev:Event) RETURN count(ev) AS n").single()["n"]
            GRAPH_ENTITIES.set(entities)
            GRAPH_EVENTS.set(events)
        except Exception:
            logger.warning("failed to refresh graph gauges for /metrics", exc_info=True)
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.websocket("/ws/claims")
    async def ws_claims(ws: WebSocket) -> None:
        await ws.accept()
        broadcaster.register(ws)
        try:
            while True:
                await ws.receive_text()  # keepalive; clients don't send data
        except WebSocketDisconnect:
            broadcaster.unregister(ws)

    # Mounted LAST so every API route above takes precedence. Skipped (with a
    # warning) when no built frontend exists — dev environments without an
    # `npm run build` must still serve the API.
    frontend_dist = _frontend_dist()
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    else:
        logger.warning("frontend dist not found at %s; serving API only", frontend_dist)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    if settings.metrics_port > 0:
        start_metrics_server(settings.metrics_port)
    uvicorn.run(create_app(start_consumer=True), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
