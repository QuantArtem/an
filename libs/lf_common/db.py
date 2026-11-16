import asyncio
import json
import logging

import asyncpg

log = logging.getLogger(__name__)

# Таблицы, одинаковые для всех сервисов: outbox (исходящие события)
# и processed_events (inbox — защита от повторной обработки).
COMMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id           BIGSERIAL PRIMARY KEY,
    event_id     UUID        NOT NULL UNIQUE,
    topic        TEXT        NOT NULL,
    msg_key      TEXT        NOT NULL,
    event_type   TEXT        NOT NULL,
    payload      JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS processed_events (
    event_id     UUID PRIMARY KEY,
    event_type   TEXT        NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, schema_sql: str = "") -> asyncpg.Pool:
    last: Exception | None = None
    for _ in range(60):
        try:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10, init=_init_conn)
            async with pool.acquire() as conn:
                await conn.execute(COMMON_SCHEMA + schema_sql)
            return pool
        except (OSError, asyncpg.PostgresError) as exc:  # БД ещё поднимается
            last = exc
            log.warning("database is not ready: %s", exc)
            await asyncio.sleep(1)
    raise RuntimeError(f"cannot connect to database: {last}")
