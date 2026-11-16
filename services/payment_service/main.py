"""payment-service — приём оплаты при получении через внешний платёжный шлюз (PSP).

Публичный контракт: docs/05-integration/openapi/payment-service.yaml
"""
import json
import logging
import os
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import Header, Request, Response
from lf_common.app import create_app
from lf_common.errors import ApiError
from lf_common.events import publish
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .domain import verify_signature, webhook_outcome

log = logging.getLogger("payment")
SERVICE = "payment-service"
TOPIC = "payment.events"
PSP_URL = os.environ.get("PSP_URL", "http://localhost:8010")
PSP_WEBHOOK_SECRET = os.environ.get("PSP_WEBHOOK_SECRET", "dev-webhook-secret")
WEBHOOK_URL = os.environ.get("PAYMENT_WEBHOOK_URL", "http://localhost:8003/api/v1/webhooks/psp")


class Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class CreatePayment(Dto):
    shipment_id: UUID


class PspWebhook(Dto):
    event_id: str = Field(min_length=1, max_length=128)
    psp_payment_id: str
    merchant_payment_id: UUID
    status: Literal["succeeded", "failed"]
    amount: int
    currency: str


def to_api(p) -> dict:
    return {"id": str(p["id"]), "shipmentId": str(p["shipment_id"]), "status": p["status"], "amount": p["amount"],
            "currency": p["currency"], "paymentUrl": p["payment_url"], "createdAt": p["created_at"].isoformat(),
            "updatedAt": p["updated_at"].isoformat()}


# ---------- Обработчики событий shipment-service ----------
async def on_shipment_created(conn, ev):
    p = ev["payload"]
    if p["paymentType"] == "ON_PICKUP":
        await conn.execute(
            "INSERT INTO invoices (shipment_id, amount, currency) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            p["shipmentId"], p["amount"], p["currency"])


async def on_shipment_closed(conn, ev):
    """Отправление отклонено / отменено / истёк срок хранения — неоплаченный счёт закрываем."""
    await conn.execute(
        "UPDATE invoices SET status = 'CANCELLED', updated_at = now() WHERE shipment_id = $1 AND status = 'OPEN'",
        ev["payload"]["shipmentId"])


HANDLERS = {
    "shipment.created": on_shipment_created,
    "shipment.rejected": on_shipment_closed,
    "shipment.cancelled": on_shipment_closed,
    "shipment.expired": on_shipment_closed,
}

app = create_app(
    title="LockerFlow Payment Service", service=SERVICE, description="Оплата отправлений при получении",
    schema_sql=(Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"),
    topics=["shipment.events"], handlers=HANDLERS,
)


@app.post("/api/v1/payments", status_code=201, tags=["payments"])
async def create_payment(body: CreatePayment, request: Request, response: Response,
                         idempotency_key: str | None = Header(default=None, max_length=128)):
    if not idempotency_key:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Заголовок Idempotency-Key обязателен")
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        # Блокировка счёта сериализует параллельные попытки оплаты одного отправления
        invoice = await conn.fetchrow("SELECT * FROM invoices WHERE shipment_id = $1 FOR UPDATE", body.shipment_id)
        if not invoice:
            raise ApiError(404, "INVOICE_NOT_FOUND", "Для отправления нет счёта к оплате")
        existing = await conn.fetchrow("SELECT * FROM payments WHERE idempotency_key = $1", idempotency_key)
        if existing:
            if existing["shipment_id"] != body.shipment_id:
                raise ApiError(422, "IDEMPOTENCY_KEY_REUSED", "Ключ идемпотентности уже использован с другим запросом")
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
            return to_api(existing)
        if invoice["status"] == "PAID":
            raise ApiError(409, "ALREADY_PAID", "Отправление уже оплачено")
        if invoice["status"] == "CANCELLED":
            raise ApiError(409, "INVOICE_CANCELLED", "Счёт закрыт: отправление отменено или срок хранения истёк")
        pending = await conn.fetchrow(
            "SELECT * FROM payments WHERE shipment_id = $1 AND status = 'PENDING' ORDER BY created_at DESC LIMIT 1",
            body.shipment_id)
        if pending:  # BR-10: не плодим параллельные платежи — отдаём уже созданный
            response.status_code = 200
            return to_api(pending)
        payment_id = uuid4()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(f"{PSP_URL}/psp/payments", json={
                    "merchantPaymentId": str(payment_id), "amount": invoice["amount"],
                    "currency": invoice["currency"], "callbackUrl": WEBHOOK_URL,
                    "description": f"Оплата отправления {body.shipment_id}"})
                r.raise_for_status()
                psp = r.json()
        except httpx.HTTPError as exc:
            log.error("PSP call failed: %s", exc)
            raise ApiError(502, "PSP_UNAVAILABLE", "Платёжный шлюз временно недоступен, повторите попытку позже")
        row = await conn.fetchrow(
            "INSERT INTO payments (id, shipment_id, amount, currency, status, psp_payment_id, payment_url, "
            "idempotency_key) VALUES ($1, $2, $3, $4, 'PENDING', $5, $6, $7) RETURNING *",
            payment_id, body.shipment_id, invoice["amount"], invoice["currency"], psp["pspPaymentId"],
            psp["paymentUrl"], idempotency_key)
        return to_api(row)


@app.get("/api/v1/payments/{payment_id}", tags=["payments"])
async def get_payment(payment_id: UUID, request: Request):
    row = await request.app.state.pool.fetchrow("SELECT * FROM payments WHERE id = $1", payment_id)
    if not row:
        raise ApiError(404, "PAYMENT_NOT_FOUND", f"Платёж {payment_id} не найден")
    return to_api(row)


@app.post("/api/v1/webhooks/psp", tags=["webhooks"])
async def psp_webhook(request: Request, x_psp_signature: str | None = Header(default=None)):
    """Уведомление PSP о результате платежа. Подписано HMAC-SHA256, обрабатывается идемпотентно."""
    raw = await request.body()
    if not verify_signature(PSP_WEBHOOK_SECRET, raw, x_psp_signature):
        raise ApiError(401, "INVALID_SIGNATURE", "Подпись уведомления не прошла проверку")
    try:
        hook = PspWebhook.model_validate_json(raw)
    except ValueError as exc:
        raise ApiError(400, "VALIDATION_ERROR", f"Некорректное тело уведомления: {exc}")
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        first_time = await conn.fetchval(
            "INSERT INTO psp_webhook_log (psp_event_id, payment_id, payload) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING RETURNING psp_event_id", hook.event_id, hook.merchant_payment_id, json.loads(raw))
        if first_time is None:
            return {"result": "DUPLICATE"}
        payment = await conn.fetchrow("SELECT * FROM payments WHERE id = $1 FOR UPDATE", hook.merchant_payment_id)
        if not payment or payment["psp_payment_id"] != hook.psp_payment_id:
            raise ApiError(404, "PAYMENT_NOT_FOUND", "Платёж из уведомления не найден")
        if (hook.amount, hook.currency) != (payment["amount"], payment["currency"]):
            raise ApiError(422, "AMOUNT_MISMATCH", "Сумма или валюта в уведомлении не совпадает с платежом")
        outcome = webhook_outcome(payment["status"], hook.status)
        if outcome == "IGNORE":
            return {"result": "IGNORED"}
        key = str(payment["shipment_id"])
        base = {"paymentId": str(payment["id"]), "shipmentId": key, "amount": payment["amount"],
                "currency": payment["currency"]}
        if outcome == "APPLY_SUCCESS":
            row = await conn.fetchrow(
                "UPDATE payments SET status = 'SUCCEEDED', updated_at = now() WHERE id = $1 RETURNING updated_at",
                payment["id"])
            await conn.execute("UPDATE invoices SET status = 'PAID', updated_at = now() WHERE shipment_id = $1",
                               payment["shipment_id"])
            await publish(conn, TOPIC, key, "payment.succeeded", {**base, "paidAt": row["updated_at"].isoformat()},
                          SERVICE)
        else:
            await conn.execute("UPDATE payments SET status = 'FAILED', updated_at = now() WHERE id = $1",
                               payment["id"])
            await publish(conn, TOPIC, key, "payment.failed", base, SERVICE)
        return {"result": "ACCEPTED"}
