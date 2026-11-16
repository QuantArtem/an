"""locker-service — постаматы, ячейки, резервы, закладка и выдача посылок.

Публичный контракт: docs/05-integration/openapi/locker-service.yaml
"""
import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import Request
from lf_common.app import create_app
from lf_common.errors import ApiError
from lf_common.events import publish
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .domain import BLOCK_MINUTES, MAX_FAILED_ATTEMPTS, generate_pickup_code, hash_pickup_code, suitable_sizes

log = logging.getLogger("locker")
SERVICE = "locker-service"
TOPIC = "locker.events"
PEPPER = os.environ.get("PICKUP_CODE_PEPPER", "dev-pepper")
RESERVATION_TTL_SECONDS = int(os.environ.get("RESERVATION_TTL_SECONDS", 5 * 24 * 3600))  # BR-04


class Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class DepositRequest(Dto):
    shipment_id: UUID
    courier_id: str = Field(min_length=1, max_length=64)


class PickupRequest(Dto):
    pickup_code: str = Field(pattern=r"^\d{6}$")


class ReturnRequest(Dto):
    shipment_id: UUID
    courier_id: str = Field(min_length=1, max_length=64)


async def emit(conn, event_type: str, shipment_id, **payload) -> None:
    await publish(conn, TOPIC, str(shipment_id), event_type, {"shipmentId": str(shipment_id), **payload}, SERVICE)


async def free_cell(conn, reservation, new_status: str) -> None:
    await conn.execute("UPDATE reservations SET status = $2, updated_at = now() WHERE id = $1",
                       reservation["id"], new_status)
    await conn.execute("UPDATE cells SET status = 'FREE' WHERE id = $1", reservation["cell_id"])


# ---------- Обработчики событий shipment-service ----------
async def on_shipment_created(conn, ev):
    p = ev["payload"]
    sid, locker_id = p["shipmentId"], p["lockerId"]

    async def fail(reason: str):
        await emit(conn, "locker.cell.reservation_failed", sid, lockerId=locker_id, reason=reason)

    locker = await conn.fetchrow("SELECT * FROM lockers WHERE id = $1", locker_id)
    if not locker:
        return await fail("LOCKER_NOT_FOUND")
    if locker["status"] != "ACTIVE":
        return await fail("LOCKER_UNAVAILABLE")
    # BR-03 + конкурентный доступ: берём минимальную подходящую свободную ячейку,
    # пропуская строки, заблокированные параллельными транзакциями.
    cell = await conn.fetchrow(
        "SELECT * FROM cells WHERE locker_id = $1 AND status = 'FREE' AND size = ANY($2::text[]) "
        "ORDER BY array_position($2::text[], size), number LIMIT 1 FOR UPDATE SKIP LOCKED",
        locker_id, suitable_sizes(p["parcelSize"]))
    if not cell:
        return await fail("NO_FREE_CELLS")
    expires = datetime.now(UTC) + timedelta(seconds=RESERVATION_TTL_SECONDS)
    await conn.execute("UPDATE cells SET status = 'RESERVED' WHERE id = $1", cell["id"])
    await conn.execute(
        "INSERT INTO reservations (shipment_id, locker_id, cell_id, status, payment_required, expires_at) "
        "VALUES ($1, $2, $3, 'RESERVED', $4, $5)",
        sid, locker_id, cell["id"], p["paymentType"] == "ON_PICKUP", expires)
    await emit(conn, "locker.cell.reserved", sid, lockerId=locker_id, lockerCode=locker["code"],
               lockerAddress=locker["address"], cellNumber=cell["number"], cellSize=cell["size"],
               reservedUntil=expires.isoformat())


async def on_shipment_cancelled(conn, ev):
    sid = ev["payload"]["shipmentId"]
    r = await conn.fetchrow("SELECT * FROM reservations WHERE shipment_id = $1 AND status = 'RESERVED' FOR UPDATE", sid)
    if r:
        await free_cell(conn, r, "RELEASED")
        await emit(conn, "locker.cell.released", sid, lockerId=str(r["locker_id"]), reason=ev["payload"].get("reason"))


async def on_shipment_paid(conn, ev):
    await conn.execute("UPDATE reservations SET payment_required = FALSE, updated_at = now() WHERE shipment_id = $1",
                       ev["payload"]["shipmentId"])


async def on_shipment_expired(conn, ev):
    # Код получения перестаёт действовать, ячейка остаётся занятой до изъятия курьером
    await conn.execute(
        "UPDATE reservations SET status = 'STORAGE_EXPIRED', updated_at = now() "
        "WHERE shipment_id = $1 AND status = 'DEPOSITED'", ev["payload"]["shipmentId"])


HANDLERS = {
    "shipment.created": on_shipment_created,
    "shipment.cancelled": on_shipment_cancelled,
    "shipment.paid": on_shipment_paid,
    "shipment.expired": on_shipment_expired,
}


# ---------- Фоновое задание: освобождение просроченных резервов (BR-04) ----------
async def reservation_ttl_job(app) -> None:
    while True:
        await asyncio.sleep(10)
        try:
            async with app.state.pool.acquire() as conn, conn.transaction():
                rows = await conn.fetch(
                    "SELECT * FROM reservations WHERE status = 'RESERVED' AND expires_at < now() "
                    "LIMIT 100 FOR UPDATE SKIP LOCKED")
                for r in rows:
                    await free_cell(conn, r, "RELEASED")
                    await emit(conn, "locker.reservation.expired", r["shipment_id"], lockerId=str(r["locker_id"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reservation ttl job failed")


app = create_app(
    title="LockerFlow Locker Service", service=SERVICE,
    description="Постаматы, ячейки, резервирование, закладка и выдача",
    schema_sql=(Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"),
    topics=["shipment.events"], handlers=HANDLERS, jobs=[reservation_ttl_job],
)

LOCKER_SQL = """
SELECT l.id, l.code, l.city, l.address, l.status,
       count(c.id) FILTER (WHERE c.status = 'FREE' AND c.size = 'S') AS free_s,
       count(c.id) FILTER (WHERE c.status = 'FREE' AND c.size = 'M') AS free_m,
       count(c.id) FILTER (WHERE c.status = 'FREE' AND c.size = 'L') AS free_l
FROM lockers l LEFT JOIN cells c ON c.locker_id = l.id
"""


def locker_api(r) -> dict:
    return {"id": str(r["id"]), "code": r["code"], "city": r["city"], "address": r["address"],
            "status": r["status"], "freeCells": {"S": r["free_s"], "M": r["free_m"], "L": r["free_l"]}}


async def get_active_locker(conn, locker_id: UUID, for_update: bool = False):
    locker = await conn.fetchrow("SELECT * FROM lockers WHERE id = $1" + (" FOR UPDATE" if for_update else ""),
                                 locker_id)
    if not locker:
        raise ApiError(404, "LOCKER_NOT_FOUND", f"Постамат {locker_id} не найден")
    if locker["status"] != "ACTIVE":
        raise ApiError(409, "LOCKER_UNAVAILABLE", f"Постамат {locker['code']} недоступен")
    return locker


@app.get("/api/v1/lockers", tags=["lockers"])
async def list_lockers(request: Request, city: str | None = None):
    rows = await request.app.state.pool.fetch(
        LOCKER_SQL + "WHERE ($1::text IS NULL OR l.city = $1) GROUP BY l.id ORDER BY l.code", city)
    return {"items": [locker_api(r) for r in rows]}


@app.get("/api/v1/lockers/{locker_id}", tags=["lockers"])
async def get_locker(locker_id: UUID, request: Request):
    row = await request.app.state.pool.fetchrow(LOCKER_SQL + "WHERE l.id = $1 GROUP BY l.id", locker_id)
    if not row:
        raise ApiError(404, "LOCKER_NOT_FOUND", f"Постамат {locker_id} не найден")
    return locker_api(row)


@app.post("/api/v1/lockers/{locker_id}/deposits", status_code=201, tags=["operations"])
async def deposit(locker_id: UUID, body: DepositRequest, request: Request):
    """Курьер закладывает посылку в забронированную ячейку."""
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        await get_active_locker(conn, locker_id)
        r = await conn.fetchrow(
            "SELECT r.*, c.number, c.size FROM reservations r JOIN cells c ON c.id = r.cell_id "
            "WHERE r.shipment_id = $1 AND r.locker_id = $2 FOR UPDATE OF r", body.shipment_id, locker_id)
        if not r:
            raise ApiError(404, "RESERVATION_NOT_FOUND", "Для отправления нет резерва в этом постамате")
        if r["status"] != "RESERVED":
            raise ApiError(409, "INVALID_RESERVATION_STATE", f"Закладка невозможна в статусе {r['status']}",
                           currentStatus=r["status"])
        # BR-07: код уникален среди посылок, лежащих в этом постамате
        while True:
            code = generate_pickup_code()
            code_hash = hash_pickup_code(PEPPER, str(locker_id), code)
            taken = await conn.fetchval(
                "SELECT 1 FROM reservations WHERE locker_id = $1 AND pickup_code_hash = $2 AND status = 'DEPOSITED'",
                locker_id, code_hash)
            if not taken:
                break
        now = datetime.now(UTC)
        await conn.execute(
            "UPDATE reservations SET status = 'DEPOSITED', pickup_code_hash = $2, courier_id = $3, updated_at = now() "
            "WHERE id = $1", r["id"], code_hash, body.courier_id)
        await conn.execute("UPDATE cells SET status = 'OCCUPIED' WHERE id = $1", r["cell_id"])
        await emit(conn, "locker.parcel.deposited", body.shipment_id, lockerId=str(locker_id),
                   cellNumber=r["number"], pickupCode=code, courierId=body.courier_id, depositedAt=now.isoformat())
        return {"shipmentId": str(body.shipment_id), "lockerId": str(locker_id), "cellNumber": r["number"],
                "cellSize": r["size"], "depositedAt": now.isoformat()}


@app.post("/api/v1/lockers/{locker_id}/pickups", tags=["operations"])
async def pickup(locker_id: UUID, body: PickupRequest, request: Request):
    """Получатель вводит код на терминале постамата — открывается ячейка."""
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        locker = await get_active_locker(conn, locker_id, for_update=True)
        now = datetime.now(UTC)
        if locker["blocked_until"] and locker["blocked_until"] > now:
            wait = int((locker["blocked_until"] - now).total_seconds()) + 1
            raise ApiError(429, "PICKUP_BLOCKED", "Слишком много неверных попыток, ввод кода временно заблокирован",
                           headers={"Retry-After": str(wait)}, retryAfterSeconds=wait)
        code_hash = hash_pickup_code(PEPPER, str(locker_id), body.pickup_code)
        r = await conn.fetchrow(
            "SELECT r.*, c.number FROM reservations r JOIN cells c ON c.id = r.cell_id "
            "WHERE r.locker_id = $1 AND r.pickup_code_hash = $2 AND r.status = 'DEPOSITED' FOR UPDATE OF r",
            locker_id, code_hash)
        if r and r["payment_required"]:
            raise ApiError(409, "PAYMENT_REQUIRED", "Посылка не оплачена. Оплатите заказ и повторите ввод кода",
                           shipmentId=str(r["shipment_id"]))
        if r:
            await free_cell(conn, r, "PICKED_UP")
            await conn.execute("UPDATE lockers SET failed_attempts = 0, blocked_until = NULL WHERE id = $1",
                               locker_id)
            await emit(conn, "locker.parcel.picked_up", r["shipment_id"], lockerId=str(locker_id),
                       cellNumber=r["number"], pickedUpAt=now.isoformat())
            return {"shipmentId": str(r["shipment_id"]), "cellNumber": r["number"], "pickedUpAt": now.isoformat()}
        # BR-08: счётчик неверных попыток фиксируется той же транзакцией (она коммитится, ошибку отдаём после)
        attempts = locker["failed_attempts"] + 1
        blocked = attempts >= MAX_FAILED_ATTEMPTS
        await conn.execute(
            "UPDATE lockers SET failed_attempts = $2, blocked_until = $3 WHERE id = $1", locker_id,
            0 if blocked else attempts, now + timedelta(minutes=BLOCK_MINUTES) if blocked else None)
    raise ApiError(403, "INVALID_PICKUP_CODE", "Неверный код получения",
                   attemptsLeft=0 if blocked else MAX_FAILED_ATTEMPTS - attempts)


@app.post("/api/v1/lockers/{locker_id}/returns", tags=["operations"])
async def return_parcel(locker_id: UUID, body: ReturnRequest, request: Request):
    """Курьер изымает посылку с истёкшим сроком хранения."""
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        await get_active_locker(conn, locker_id)
        r = await conn.fetchrow(
            "SELECT r.*, c.number FROM reservations r JOIN cells c ON c.id = r.cell_id "
            "WHERE r.shipment_id = $1 AND r.locker_id = $2 FOR UPDATE OF r", body.shipment_id, locker_id)
        if not r:
            raise ApiError(404, "RESERVATION_NOT_FOUND", "Для отправления нет резерва в этом постамате")
        if r["status"] != "STORAGE_EXPIRED":
            raise ApiError(409, "INVALID_RESERVATION_STATE", f"Изъятие невозможно в статусе {r['status']}",
                           currentStatus=r["status"])
        now = datetime.now(UTC)
        await free_cell(conn, r, "RETURNED")
        await emit(conn, "locker.parcel.returned", body.shipment_id, lockerId=str(locker_id),
                   cellNumber=r["number"], courierId=body.courier_id, returnedAt=now.isoformat())
        return {"shipmentId": str(body.shipment_id), "cellNumber": r["number"], "returnedAt": now.isoformat()}


@app.post("/internal/v1/lockers/{locker_id}/unblock", tags=["internal"])
async def unblock(locker_id: UUID, request: Request):
    """Операторское действие: снять блокировку ввода кода (не публикуется через шлюз)."""
    updated = await request.app.state.pool.fetchval(
        "UPDATE lockers SET failed_attempts = 0, blocked_until = NULL WHERE id = $1 RETURNING id", locker_id)
    if not updated:
        raise ApiError(404, "LOCKER_NOT_FOUND", f"Постамат {locker_id} не найден")
    return {"lockerId": str(locker_id), "blocked": False}
