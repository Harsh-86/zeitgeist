"""API service: health, stats, and live claim fan-out over websockets."""

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from zeitgeist.config import CLAIMS_TOPIC, Settings
from zeitgeist.kafka_utils import make_consumer

logger = logging.getLogger("zeitgeist.api")

_DEFAULT_DASHBOARD_INDEX = Path(__file__).resolve().parents[3] / "dashboard" / "index.html"


def _dashboard_index() -> Path:
    """Resolve the dashboard entry point, overridable via DASHBOARD_PATH.

    In the Docker image the package is pip-installed into site-packages, so the
    source-tree-relative default (parents[3]/dashboard/index.html) does not
    resolve to /app/dashboard. Compose sets DASHBOARD_PATH=/app/dashboard/index.html
    in that environment.
    """
    override = os.getenv("DASHBOARD_PATH")
    return Path(override) if override else _DEFAULT_DASHBOARD_INDEX


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


def create_app(driver=None, start_consumer: bool = False) -> FastAPI:
    broadcaster = Broadcaster()

    if driver is None:
        from neo4j import GraphDatabase

        settings = Settings.from_env()
        driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
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

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_dashboard_index())

    @app.websocket("/ws/claims")
    async def ws_claims(ws: WebSocket) -> None:
        await ws.accept()
        broadcaster.register(ws)
        try:
            while True:
                await ws.receive_text()  # keepalive; clients don't send data
        except WebSocketDisconnect:
            broadcaster.unregister(ws)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    uvicorn.run(create_app(start_consumer=True), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
