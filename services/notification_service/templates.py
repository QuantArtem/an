"""Шаблоны уведомлений получателю. Чистый модуль, покрыт unit-тестами."""
from datetime import datetime

PAY_URL = "https://pay.lockerflow.example/s/{shipmentId}"


def _rub(kopecks: int) -> str:
    return f"{kopecks // 100} руб. {kopecks % 100:02d} коп."


def _dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")


def render(event_type: str, p: dict) -> tuple[str, str] | None:
    """Возвращает (код шаблона, текст) или None, если по событию уведомление не отправляется."""
    if event_type == "shipment.ready_for_pickup":
        text = (f"Заказ {p['externalOrderId']} ждёт вас в постамате: {p['lockerAddress']}. "
                f"Код получения: {p['pickupCode']}. Хранение до {_dt(p['storageUntil'])} (UTC).")
        if p.get("paymentRequired"):
            text += f" К оплате {_rub(p['amount'])}: " + PAY_URL.format(shipmentId=p["shipmentId"])
        return "READY_FOR_PICKUP", text
    if event_type == "shipment.paid":
        return "PAYMENT_RECEIVED", (f"Оплата {_rub(p['amount'])} по заказу {p['externalOrderId']} получена. "
                                    "Введите код получения на экране постамата.")
    if event_type == "shipment.delivered":
        return "DELIVERED", f"Заказ {p['externalOrderId']} получен. Спасибо, что пользуетесь LockerFlow!"
    if event_type == "shipment.expired":
        return "STORAGE_EXPIRED", (f"Срок хранения заказа {p['externalOrderId']} истёк. "
                                   "Заказ будет возвращён в магазин.")
    return None
