"""Фабрика приложения: общий жизненный цикл для всех сервисов."""
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request

from .context import correlation_id
from .db import create_pool
from .errors import install_error_handlers
from .events import EventConsumer, OutboxRelay, start_producer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("aiokafka").setLevel(logging.WARNING)

Job = Callable[[FastAPI], Awaitable[None]]


def create_app(*, title: str, service: str, schema_sql: str, description: str = "",
               topics: list[str] | None = None, handlers: dict | None = None,
               jobs: list[Job] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bootstrap = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
        app.state.pool = await create_pool(os.environ["DATABASE_URL"], schema_sql)
        app.state.producer = await start_producer(bootstrap)
        tasks = [asyncio.create_task(OutboxRelay(app.state.pool, app.state.producer).run())]
        if topics:
            consumer = EventConsumer(app.state.pool, app.state.producer, bootstrap, service, topics, handlers or {})
            tasks.append(asyncio.create_task(consumer.run()))
        tasks += [asyncio.create_task(job(app)) for job in (jobs or [])]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.producer.stop()
            await app.state.pool.close()

    app = FastAPI(title=title, version="1.0.0", description=description, lifespan=lifespan)
    install_error_handlers(app)

    @app.middleware("http")
    async def _correlation(request: Request, call_next):
        cid = request.headers.get("X-Correlation-Id") or str(uuid4())
        correlation_id.set(cid)
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        return response

    @app.get("/health", include_in_schema=False)
    async def health():
        await app.state.pool.fetchval("SELECT 1")
        return {"status": "UP", "service": service}

    return app
