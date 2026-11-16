"""shipment-service — ядро системы: жизненный цикл отправления.

Публичный контракт: docs/05-integration/openapi/shipment-service.yaml
"""
import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, Header, Query, Request, Response
from lf_common.app import create_app
from lf_common.errors import ApiError
from lf_common.events import publish
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from pydantic.alias_generators import to_camel

from .domain import PaymentStatus, Status, can_transition, initial_payment_status

log = logging.getLogger("shipment")
SERVICE = "shipment-service"
TOPIC = "shipment.events"
STORAGE_TTL_SECONDS = int(os.environ.get("STORAGE_TTL_SECONDS", 72 * 3600))  # BR-05: срок хранения 72 часа


# ---------- DTO ----------
class Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Recipient(Dto):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(pattern=r"^\+\d{10,15}$", description="E.164")
    email: EmailStr | None = None


class Payment(Dto):
    type: Literal["PREPAID", "ON_PICKUP"]
    amount: int = Field(default=0, ge=0, le=50_000_000, description="Сумма к оплате в копейках")
    currency: Literal["RUB"] = "RUB"

    @model_validator(mode="after")
    def _check(self):
        if self.type == "ON_PICKUP" and self.amount <= 0:
            raise ValueError("amount must be positive for ON_PICKUP payment")
        if self.type == "PREPAID" and self.amount != 0:
            raise ValueError("amount must be 0 for PREPAID payment")
        return self


class CreateShipment(Dto):
    external_order_id: str = Field(min_length=1, max_length=64)
    locker_id: UUID
    parcel_size: Literal["S", "M", "L"]
    recipient: Recipient
    payment: Payment


class CancelRequest(Dto):
    reason: str | None = Field(default=None, max_length=500)


def to_api(r) -> dict:
    return {
        "id": str(r["id"]),
        "externalOrderId": r["external_order_id"],
        "status": r["status"],
        "statusReason": r["status_reason"],
        "paymentStatus": r["payment_status"],
        "lockerId": str(r["locker_id"]),
        "lockerAddress": r["locker_address"],
        "cellNumber": r["cell_number"],
        "parcelSize": r["parcel_size"],
        "recipient": {"name": r["recipient_name"], "phone": r["recipient_phone"], "email": r["recipient_email"]},
        "payment": {"type": r["payment_type"], "amount": r["amount"], "currency": r["currency"]},
        "storageUntil": r["storage_until"].isoformat() if r["storage_until"] else None,
        "createdAt": r["created_at"].isoformat(),
        "updatedAt": r["updated_at"].isoformat(),
    }


# ---------- Доменные операции (всегда внутри транзакции) ----------
async def transition(conn, shipment, to: Status, *, actor: str, reason: str | None = None, **fields):
    """Меняет статус с проверкой по таблице переходов и пишет историю. Возвращает обновлённую строку или None."""
    if not can_transition(shipment["status"], to):
        log.warning("transition %s -> %s ignored for %s", shipment["status"], to, shipment["id"])
        return None
    cols = {"status": str(to), "status_reason": reason, **fields}
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    row = await conn.fetchrow(
        f"UPDATE shipments SET {sets}, updated_at = now() WHERE id = $1 RETURNING *", shipment["id"], *cols.values()
    )
    await conn.execute(
        "INSERT INTO shipment_status_history (shipment_id, from_status, to_status, reason, actor) "
        "VALUES ($1, $2, $3, $4, $5)", shipment["id"], shipment["status"], str(to), reason, actor,
    )
    return row


async def emit(conn, event_type: str, s, **extra) -> None:
    payload = {"shipmentId": str(s["id"]), "partnerId": str(s["partner_id"]),
               "externalOrderId": s["external_order_id"], "lockerId": str(s["locker_id"]), **extra}
    await publish(conn, TOPIC, str(s["id"]), event_type, payload, SERVICE)


def recipient_of(s) -> dict:
    return {"name": s["recipient_name"], "phone": s["recipient_phone"], "email": s["recipient_email"]}


async def lock(conn, shipment_id):
    return await conn.fetchrow("SELECT * FROM shipments WHERE id = $1 FOR UPDATE", shipment_id)


async def expire(conn, s, actor: str) -> bool:
    row = await transition(conn, s, Status.EXPIRED, actor=actor, reason="STORAGE_EXPIRED")
    if row:
        await emit(conn, "shipment.expired", row, recipient=recipient_of(row), lockerAddress=row["locker_address"])
    return bool(row)


# ---------- Обработчики событий других сервисов ----------
async def on_cell_reserved(conn, ev):
    p = ev["payload"]
    s = await lock(conn, p["shipmentId"])
    if s:
        await transition(conn, s, Status.CELL_RESERVED, actor="locker-service",
                         cell_number=p["cellNumber"], locker_address=p["lockerAddress"])


async def on_reservation_failed(conn, ev):
    p = ev["payload"]
    s = await lock(conn, p["shipmentId"])
    if s and (row := await transition(conn, s, Status.REJECTED, actor="locker-service", reason=p["reason"])):
        await emit(conn, "shipment.rejected", row, reason=p["reason"])


async def on_deposited(conn, ev):
    p = ev["payload"]
    s = await lock(conn, p["shipmentId"])
    if not s:
        return
    until = datetime.now(UTC) + timedelta(seconds=STORAGE_TTL_SECONDS)
    if row := await transition(conn, s, Status.READY_FOR_PICKUP, actor="locker-service", storage_until=until):
        await emit(conn, "shipment.ready_for_pickup", row, recipient=recipient_of(row),
                   pickupCode=p["pickupCode"], lockerAddress=row["locker_address"], cellNumber=row["cell_number"],
                   storageUntil=until.isoformat(), paymentRequired=row["payment_status"] == PaymentStatus.AWAITING,
                   amount=row["amount"], currency=row["currency"])


async def on_picked_up(conn, ev):
    s = await lock(conn, ev["payload"]["shipmentId"])
    if s and (row := await transition(conn, s, Status.DELIVERED, actor="locker-service")):
        await emit(conn, "shipment.delivered", row, recipient=recipient_of(row),
                   deliveredAt=ev["payload"]["pickedUpAt"])


async def on_returned(conn, ev):
    s = await lock(conn, ev["payload"]["shipmentId"])
    if s and (row := await transition(conn, s, Status.RETURNED, actor="locker-service")):
        await emit(conn, "shipment.returned", row)


async def on_reservation_expired(conn, ev):
    s = await lock(conn, ev["payload"]["shipmentId"])
    if s and (row := await transition(conn, s, Status.CANCELLED, actor="locker-service",
                                      reason="RESERVATION_EXPIRED")):
        await emit(conn, "shipment.cancelled", row, reason="RESERVATION_EXPIRED")


async def on_payment_succeeded(conn, ev):
    p = ev["payload"]
    s = await lock(conn, p["shipmentId"])
    if s and s["payment_status"] == PaymentStatus.AWAITING:
        row = await conn.fetchrow(
            "UPDATE shipments SET payment_status = 'PAID', updated_at = now() WHERE id = $1 RETURNING *", s["id"])
        await emit(conn, "shipment.paid", row, recipient=recipient_of(row), amount=p["amount"],
                   currency=p["currency"], paymentId=p["paymentId"])


HANDLERS = {
    "locker.cell.reserved": on_cell_reserved,
    "locker.cell.reservation_failed": on_reservation_failed,
    "locker.parcel.deposited": on_deposited,
    "locker.parcel.picked_up": on_picked_up,
    "locker.parcel.returned": on_returned,
    "locker.reservation.expired": on_reservation_expired,
    "payment.succeeded": on_payment_succeeded,
}


# ---------- Фоновое задание: контроль срока хранения (BR-05) ----------
async def storage_expiry_job(app) -> None:
    while True:
        await asyncio.sleep(5)
        try:
            async with app.state.pool.acquire() as conn, conn.transaction():
                rows = await conn.fetch(
                    "SELECT * FROM shipments WHERE status = 'READY_FOR_PICKUP' AND storage_until < now() "
                    "ORDER BY storage_until LIMIT 100 FOR UPDATE SKIP LOCKED")
                for s in rows:
                    await expire(conn, s, actor="system")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("storage expiry job failed")


app = create_app(
    title="LockerFlow Shipment Service", service=SERVICE,
    description="Управление жизненным циклом отправлений",
    schema_sql=(Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"),
    topics=["locker.events", "payment.events"], handlers=HANDLERS, jobs=[storage_expiry_job],
)


# ---------- REST API ----------
async def current_partner(request: Request, x_api_key: str | None = Header(default=None)):
    if not x_api_key:
        raise ApiError(401, "UNAUTHORIZED", "Не передан заголовок X-API-Key")
    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
    partner = await request.app.state.pool.fetchrow("SELECT * FROM partners WHERE api_key_hash = $1", key_hash)
    if not partner:
        raise ApiError(401, "UNAUTHORIZED", "Неизвестный API-ключ")
    if partner["status"] != "ACTIVE":
        raise ApiError(403, "PARTNER_BLOCKED", "Партнёр заблокирован")
    return partner


async def get_owned(conn, shipment_id: UUID, partner, for_update: bool = False):
    sql = "SELECT * FROM shipments WHERE id = $1 AND partner_id = $2" + (" FOR UPDATE" if for_update else "")
    s = await conn.fetchrow(sql, shipment_id, partner["id"])
    if not s:
        raise ApiError(404, "SHIPMENT_NOT_FOUND", f"Отправление {shipment_id} не найдено")
    return s


@app.post("/api/v1/shipments", status_code=202, tags=["shipments"])
async def create_shipment(body: CreateShipment, request: Request, response: Response,
                          partner=Depends(current_partner),
                          idempotency_key: str | None = Header(default=None, max_length=128)):
    if not idempotency_key:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Заголовок Idempotency-Key обязателен")
    req_hash = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        # Сериализуем конкурентные запросы одного партнёра с одним ключом
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{partner['id']}:{idempotency_key}")
        known = await conn.fetchrow(
            "SELECT * FROM idempotency_keys WHERE partner_id = $1 AND idem_key = $2", partner["id"], idempotency_key)
        if known:
            if known["request_hash"] != req_hash:
                raise ApiError(422, "IDEMPOTENCY_KEY_REUSED",
                               "Ключ идемпотентности уже использован с другим телом запроса")
            response.headers["Idempotent-Replayed"] = "true"
            return to_api(await conn.fetchrow("SELECT * FROM shipments WHERE id = $1", known["shipment_id"]))
        duplicate = await conn.fetchval(
            "SELECT id FROM shipments WHERE partner_id = $1 AND external_order_id = $2",
            partner["id"], body.external_order_id)
        if duplicate:
            raise ApiError(409, "DUPLICATE_EXTERNAL_ORDER",
                           f"Отправление для заказа {body.external_order_id} уже существует",
                           shipmentId=str(duplicate))
        s = await conn.fetchrow(
            "INSERT INTO shipments (id, partner_id, external_order_id, locker_id, parcel_size, recipient_name, "
            "recipient_phone, recipient_email, payment_type, amount, currency, status, payment_status) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'CREATED',$12) RETURNING *",
            uuid4(), partner["id"], body.external_order_id, body.locker_id, body.parcel_size, body.recipient.name,
            body.recipient.phone, body.recipient.email, body.payment.type, body.payment.amount,
            body.payment.currency, str(initial_payment_status(body.payment.type)),
        )
        await conn.execute(
            "INSERT INTO shipment_status_history (shipment_id, to_status, actor) VALUES ($1, 'CREATED', 'partner')",
            s["id"])
        await conn.execute(
            "INSERT INTO idempotency_keys (partner_id, idem_key, request_hash, shipment_id) VALUES ($1,$2,$3,$4)",
            partner["id"], idempotency_key, req_hash, s["id"])
        await emit(conn, "shipment.created", s, parcelSize=s["parcel_size"], paymentType=s["payment_type"],
                   amount=s["amount"], currency=s["currency"])
    response.headers["Location"] = f"/api/v1/shipments/{s['id']}"
    return to_api(s)


@app.get("/api/v1/shipments", tags=["shipments"])
async def list_shipments(request: Request, partner=Depends(current_partner),
                         status: Status | None = None,
                         external_order_id: str | None = Query(default=None, alias="externalOrderId"),
                         limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    where, args = ["partner_id = $1"], [partner["id"]]
    if status:
        args.append(str(status)); where.append(f"status = ${len(args)}")
    if external_order_id:
        args.append(external_order_id); where.append(f"external_order_id = ${len(args)}")
    cond = " AND ".join(where)
    pool = request.app.state.pool
    total = await pool.fetchval(f"SELECT count(*) FROM shipments WHERE {cond}", *args)
    rows = await pool.fetch(
        f"SELECT * FROM shipments WHERE {cond} ORDER BY created_at DESC LIMIT {limit} OFFSET {offset}", *args)
    return {"items": [to_api(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/shipments/{shipment_id}", tags=["shipments"])
async def get_shipment(shipment_id: UUID, request: Request, partner=Depends(current_partner)):
    return to_api(await get_owned(request.app.state.pool, shipment_id, partner))


@app.get("/api/v1/shipments/{shipment_id}/history", tags=["shipments"])
async def get_history(shipment_id: UUID, request: Request, partner=Depends(current_partner)):
    pool = request.app.state.pool
    await get_owned(pool, shipment_id, partner)
    rows = await pool.fetch("SELECT * FROM shipment_status_history WHERE shipment_id = $1 ORDER BY id", shipment_id)
    return {"items": [{"fromStatus": r["from_status"], "toStatus": r["to_status"], "reason": r["reason"],
                       "actor": r["actor"], "createdAt": r["created_at"].isoformat()} for r in rows]}


@app.post("/api/v1/shipments/{shipment_id}/cancel", tags=["shipments"])
async def cancel_shipment(shipment_id: UUID, request: Request, body: CancelRequest | None = None,
                          partner=Depends(current_partner)):
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        s = await get_owned(conn, shipment_id, partner, for_update=True)
        if s["status"] == Status.CANCELLED:
            return to_api(s)  # повторная отмена идемпотентна
        reason = (body.reason if body else None) or "CANCELLED_BY_PARTNER"
        row = await transition(conn, s, Status.CANCELLED, actor="partner", reason=reason)
        if not row:
            raise ApiError(409, "INVALID_SHIPMENT_STATE",
                           f"Отмена невозможна в статусе {s['status']}", currentStatus=s["status"])
        await emit(conn, "shipment.cancelled", row, reason=reason)
        return to_api(row)


@app.get("/api/v1/tracking/{shipment_id}", tags=["tracking"])
async def track(shipment_id: UUID, request: Request):
    """Публичное отслеживание для получателя: без персональных данных."""
    s = await request.app.state.pool.fetchrow("SELECT * FROM shipments WHERE id = $1", shipment_id)
    if not s:
        raise ApiError(404, "SHIPMENT_NOT_FOUND", f"Отправление {shipment_id} не найдено")
    return {"id": str(s["id"]), "status": s["status"], "paymentStatus": s["payment_status"],
            "lockerAddress": s["locker_address"],
            "storageUntil": s["storage_until"].isoformat() if s["storage_until"] else None,
            "amountDue": s["amount"] if s["payment_status"] == PaymentStatus.AWAITING else 0,
            "currency": s["currency"]}


@app.post("/internal/v1/shipments/{shipment_id}/expire", tags=["internal"])
async def force_expire(shipment_id: UUID, request: Request):
    """Операторское действие: досрочно завершить хранение (не публикуется через шлюз)."""
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        s = await lock(conn, shipment_id)
        if not s:
            raise ApiError(404, "SHIPMENT_NOT_FOUND", f"Отправление {shipment_id} не найдено")
        if not await expire(conn, s, actor="operator"):
            raise ApiError(409, "INVALID_SHIPMENT_STATE",
                           f"Завершение хранения невозможно в статусе {s['status']}", currentStatus=s["status"])
        return to_api(await conn.fetchrow("SELECT * FROM shipments WHERE id = $1", shipment_id))
