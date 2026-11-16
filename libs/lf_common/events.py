"""Событийное взаимодействие: transactional outbox + идемпотентный потребитель.

Гарантия доставки — at-least-once. Дубликаты отсекаются на стороне потребителя
по eventId (таблица processed_events), поэтому обработчики можно считать
effectively-once.
"""
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError

from .context import correlation_id

log = logging.getLogger(__name__)

DLQ_TOPIC = "lockerflow.dlq"
Handler = Callable[[asyncpg.Connection, dict], Awaitable[None]]


def make_envelope(event_type: str, key: str, payload: dict, source: str) -> dict:
    """Единый конверт события (см. docs/05-integration/asyncapi)."""
    return {
        "eventId": str(uuid4()),
        "eventType": event_type,
        "eventVersion": 1,
        "occurredAt": datetime.now(UTC).isoformat(),
        "source": source,
        "correlationId": correlation_id.get() or str(uuid4()),
        "key": key,
        "payload": payload,
    }


async def publish(conn: asyncpg.Connection, topic: str, key: str, event_type: str, payload: dict, source: str) -> None:
    """Кладёт событие в outbox В ТОЙ ЖЕ транзакции, что и изменение данных."""
    env = make_envelope(event_type, key, payload, source)
    await conn.execute(
        "INSERT INTO outbox (event_id, topic, msg_key, event_type, payload) VALUES ($1, $2, $3, $4, $5)",
        env["eventId"], topic, key, event_type, env,
    )


async def start_producer(bootstrap: str) -> AIOKafkaProducer:
    while True:
        producer = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all", enable_idempotence=True)
        try:
            await producer.start()
            return producer
        except KafkaError as exc:
            log.warning("kafka is not ready: %s", exc)
            await producer.stop()
            await asyncio.sleep(2)


class OutboxRelay:
    """Фоновая задача: вычитывает неопубликованные события и отправляет в Kafka."""

    def __init__(self, pool: asyncpg.Pool, producer: AIOKafkaProducer, interval: float = 0.2):
        self.pool, self.producer, self.interval = pool, producer, interval

    async def flush(self) -> int:
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id, topic, msg_key, payload FROM outbox WHERE published_at IS NULL "
                "ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED"
            )
            for r in rows:
                await self.producer.send_and_wait(
                    r["topic"], key=r["msg_key"].encode(), value=json.dumps(r["payload"]).encode()
                )
            if rows:
                await conn.execute(
                    "UPDATE outbox SET published_at = now() WHERE id = ANY($1::bigint[])", [r["id"] for r in rows]
                )
            return len(rows)

    async def run(self) -> None:
        while True:
            try:
                sent = await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox relay failed, will retry")
                sent = 0
                await asyncio.sleep(1)
            if not sent:
                await asyncio.sleep(self.interval)


class EventConsumer:
    """Потребитель с ручным коммитом offset, дедупликацией, ретраями и DLQ."""

    MAX_ATTEMPTS = 3

    def __init__(self, pool, producer, bootstrap: str, group_id: str, topics: list[str], handlers: dict[str, Handler]):
        self.pool, self.producer, self.bootstrap = pool, producer, bootstrap
        self.group_id, self.topics, self.handlers = group_id, topics, handlers

    async def handle(self, event: dict) -> None:
        handler = self.handlers.get(event.get("eventType"))
        if handler is None:
            return  # событие сервису не интересно
        correlation_id.set(event.get("correlationId", ""))
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                async with self.pool.acquire() as conn, conn.transaction():
                    first_time = await conn.fetchval(
                        "INSERT INTO processed_events (event_id, event_type) VALUES ($1, $2) "
                        "ON CONFLICT DO NOTHING RETURNING event_id",
                        event["eventId"], event["eventType"],
                    )
                    if first_time is None:
                        log.info("duplicate event %s skipped", event["eventId"])
                        return
                    await handler(conn, event)
                log.info("event handled type=%s key=%s corr=%s", event["eventType"], event.get("key"),
                         event.get("correlationId"))
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("handler failed type=%s attempt=%s", event.get("eventType"), attempt)
                await asyncio.sleep(0.5 * attempt)
        await self.producer.send_and_wait(
            DLQ_TOPIC, key=str(event.get("key", "")).encode(),
            value=json.dumps({"consumer": self.group_id, "event": event}).encode(),
        )
        log.error("event %s moved to DLQ", event.get("eventId"))

    async def run(self) -> None:
        while True:
            consumer = AIOKafkaConsumer(
                *self.topics, bootstrap_servers=self.bootstrap, group_id=self.group_id,
                enable_auto_commit=False, auto_offset_reset="earliest",
            )
            try:
                await consumer.start()
                async for msg in consumer:
                    await self.handle(json.loads(msg.value))
                    await consumer.commit()
            except asyncio.CancelledError:
                await consumer.stop()
                raise
            except Exception:
                log.exception("consumer crashed, restarting")
                await consumer.stop()
                await asyncio.sleep(2)
