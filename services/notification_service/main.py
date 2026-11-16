"""notification-service — уведомления получателю по событиям отправления.

Публичный контракт: docs/05-integration/openapi/notification-service.yaml
"""
import logging
from pathlib import Path
from uuid import UUID

from fastapi import Query, Request
from lf_common.app import create_app

from .templates import render

log = logging.getLogger("notification")
SERVICE = "notification-service"


async def send_sms(phone: str, text: str) -> bool:
    """Адаптер SMS-провайдера. В прототипе — заглушка: пишет сообщение в лог."""
    log.info("SMS -> %s: %s", phone, text)
    return True


async def on_shipment_event(conn, ev):
    p = ev["payload"]
    rendered = render(ev["eventType"], p)
    if not rendered:
        return
    template, text = rendered
    phone = p["recipient"]["phone"]
    ok = await send_sms(phone, text)
    await conn.execute(
        "INSERT INTO notifications (shipment_id, event_id, channel, recipient, template, body, status) "
        "VALUES ($1, $2, 'SMS', $3, $4, $5, $6) ON CONFLICT DO NOTHING",
        p["shipmentId"], ev["eventId"], phone, template, text, "SENT" if ok else "FAILED")


HANDLERS = {t: on_shipment_event for t in
            ("shipment.ready_for_pickup", "shipment.paid", "shipment.delivered", "shipment.expired")}

app = create_app(
    title="LockerFlow Notification Service", service=SERVICE, description="Уведомления получателям",
    schema_sql=(Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"),
    topics=["shipment.events"], handlers=HANDLERS,
)


@app.get("/api/v1/notifications", tags=["notifications"])
async def list_notifications(request: Request, shipment_id: UUID = Query(alias="shipmentId")):
    """Журнал уведомлений по отправлению (для поддержки; в прототипе — ещё и «телефон получателя»)."""
    rows = await request.app.state.pool.fetch(
        "SELECT * FROM notifications WHERE shipment_id = $1 ORDER BY created_at", shipment_id)
    return {"items": [{"id": str(r["id"]), "shipmentId": str(r["shipment_id"]), "channel": r["channel"],
                       "recipient": r["recipient"], "template": r["template"], "body": r["body"],
                       "status": r["status"], "createdAt": r["created_at"].isoformat()} for r in rows]}
