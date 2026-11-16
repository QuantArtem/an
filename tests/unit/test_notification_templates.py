from notification_service.templates import render

BASE = {"shipmentId": "11111111-2222-4333-8444-555555555555", "externalOrderId": "ORD-1",
        "lockerAddress": "Москва, ул. Тверская, 7", "pickupCode": "042137",
        "storageUntil": "2026-03-04T10:30:00+00:00", "amount": 149900, "currency": "RUB"}


def test_ready_for_pickup_prepaid():
    template, text = render("shipment.ready_for_pickup", {**BASE, "paymentRequired": False})
    assert template == "READY_FOR_PICKUP"
    assert "042137" in text and "Тверская" in text and "04.03 10:30" in text
    assert "К оплате" not in text


def test_ready_for_pickup_with_payment_link():
    _, text = render("shipment.ready_for_pickup", {**BASE, "paymentRequired": True})
    assert "К оплате 1499 руб. 00 коп." in text
    assert "https://pay.lockerflow.example/s/11111111-2222-4333-8444-555555555555" in text


def test_other_templates():
    assert render("shipment.paid", BASE)[0] == "PAYMENT_RECEIVED"
    assert render("shipment.delivered", BASE)[0] == "DELIVERED"
    assert render("shipment.expired", BASE)[0] == "STORAGE_EXPIRED"


def test_unknown_event_is_ignored():
    assert render("shipment.created", BASE) is None
