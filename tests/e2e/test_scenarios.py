"""Сквозные сценарии на поднятом стенде (docker compose up).

Запуск:  pytest tests/e2e -v
Каждый тест соответствует сценарию из docs/06-quality/test-cases.md.
"""
import hashlib
import hmac
import json
import os
import re
import time
from uuid import uuid4

import httpx
import pytest

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8080")
PSP = os.environ.get("PSP_URL", "http://localhost:8010")
SHIPMENT_INTERNAL = os.environ.get("SHIPMENT_INTERNAL_URL", "http://localhost:8001")
LOCKER_INTERNAL = os.environ.get("LOCKER_INTERNAL_URL", "http://localhost:8002")
WEBHOOK_SECRET = os.environ.get("PSP_WEBHOOK_SECRET", "local-stand-webhook-secret")
AUTH = {"X-API-Key": "demo-partner-key"}

MSK_001 = "11111111-1111-4111-8111-111111111111"
SPB_001 = "22222222-2222-4222-8222-222222222222"
MSK_002 = "33333333-3333-4333-8333-333333333333"   # единственная ячейка
KZN_001 = "44444444-4444-4444-8444-444444444444"   # OFFLINE

api = httpx.Client(base_url=GATEWAY, timeout=10)


def new_shipment(locker=MSK_001, size="S", pay_type="PREPAID", amount=0, **overrides):
    body = {"externalOrderId": f"ORD-{uuid4().hex[:10]}", "lockerId": locker, "parcelSize": size,
            "recipient": {"name": "Иван Петров", "phone": "+79161234567"},
            "payment": {"type": pay_type, "amount": amount}, **overrides}
    return body


def create(body, key=None, expect=202):
    r = api.post("/api/v1/shipments", json=body, headers={**AUTH, "Idempotency-Key": key or str(uuid4())})
    assert r.status_code == expect, r.text
    return r.json()


def get(shipment_id):
    r = api.get(f"/api/v1/shipments/{shipment_id}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def wait_for(fn, predicate, timeout=20.0, what="condition"):
    """Система eventually consistent — ждём, пока событие дойдёт до потребителя."""
    deadline = time.time() + timeout
    while True:
        value = fn()
        if predicate(value):
            return value
        if time.time() > deadline:
            pytest.fail(f"timeout waiting for {what}; last value: {value}")
        time.sleep(0.2)


def wait_status(shipment_id, status):
    return wait_for(lambda: get(shipment_id), lambda s: s["status"] == status, what=f"status {status}")


def notifications(shipment_id, template):
    def fetch():
        r = api.get("/api/v1/notifications", params={"shipmentId": shipment_id})
        return [n for n in r.json()["items"] if n["template"] == template]
    return wait_for(fetch, bool, what=f"notification {template}")


def deposit(shipment_id, locker=MSK_001, expect=201):
    r = api.post(f"/api/v1/lockers/{locker}/deposits", json={"shipmentId": shipment_id, "courierId": "courier-7"})
    assert r.status_code == expect, r.text
    return r.json()


def pickup(code, locker=MSK_001):
    return api.post(f"/api/v1/lockers/{locker}/pickups", json={"pickupCode": code})


def pickup_code_from_sms(shipment_id):
    sms = notifications(shipment_id, "READY_FOR_PICKUP")[0]
    return re.search(r"Код получения: (\d{6})", sms["body"]).group(1)


def to_ready(**kw):
    s = create(new_shipment(**kw))
    wait_status(s["id"], "CELL_RESERVED")
    deposit(s["id"], kw.get("locker", MSK_001))
    wait_status(s["id"], "READY_FOR_PICKUP")
    return s["id"], pickup_code_from_sms(s["id"])


def reset_pickup_block(locker, good_code):
    """Успешная выдача сбрасывает счётчик неверных попыток."""
    assert pickup(good_code, locker).status_code == 200


# ---------- TC-01: основной сценарий, предоплаченный заказ ----------
def test_tc01_prepaid_happy_path():
    s = create(new_shipment(size="M"))
    assert s["status"] == "CREATED" and s["paymentStatus"] == "NOT_REQUIRED"

    reserved = wait_status(s["id"], "CELL_RESERVED")
    assert reserved["cellNumber"] and reserved["lockerAddress"] == "Москва, ул. Тверская, 7"

    dep = deposit(s["id"])
    assert dep["cellNumber"] == reserved["cellNumber"] and dep["cellSize"] == "M"

    ready = wait_status(s["id"], "READY_FOR_PICKUP")
    assert ready["storageUntil"]

    r = pickup(pickup_code_from_sms(s["id"]))
    assert r.status_code == 200 and r.json()["shipmentId"] == s["id"]

    wait_status(s["id"], "DELIVERED")
    notifications(s["id"], "DELIVERED")
    history = api.get(f"/api/v1/shipments/{s['id']}/history", headers=AUTH).json()["items"]
    assert [h["toStatus"] for h in history] == ["CREATED", "CELL_RESERVED", "READY_FOR_PICKUP", "DELIVERED"]


# ---------- TC-02: оплата при получении ----------
def test_tc02_pay_on_pickup():
    sid, code = to_ready(pay_type="ON_PICKUP", amount=149900)
    assert "К оплате 1499 руб." in notifications(sid, "READY_FOR_PICKUP")[0]["body"]
    assert api.get(f"/api/v1/tracking/{sid}").json()["amountDue"] == 149900

    # Без оплаты ячейка не открывается
    r = pickup(code)
    assert r.status_code == 409 and r.json()["code"] == "PAYMENT_REQUIRED"
    assert r.headers["content-type"].startswith("application/problem+json")

    key = str(uuid4())
    p = api.post("/api/v1/payments", json={"shipmentId": sid}, headers={"Idempotency-Key": key})
    assert p.status_code == 201 and p.json()["status"] == "PENDING" and p.json()["amount"] == 149900
    payment = p.json()

    # Повтор запроса с тем же ключом не создаёт второй платёж
    again = api.post("/api/v1/payments", json={"shipmentId": sid}, headers={"Idempotency-Key": key})
    assert again.status_code == 200 and again.json()["id"] == payment["id"]

    # Плательщик оплачивает на стороне PSP; PSP присылает webhook дважды
    psp_id = payment["paymentUrl"].rsplit("/", 1)[1]
    c = httpx.post(f"{PSP}/psp/payments/{psp_id}/confirm", json={"outcome": "succeeded", "duplicate": True})
    assert c.json()["webhookStatusCodes"] == [200, 200]

    wait_for(lambda: get(sid), lambda s: s["paymentStatus"] == "PAID", what="paymentStatus PAID")
    assert api.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "SUCCEEDED"
    assert len(notifications(sid, "PAYMENT_RECEIVED")) == 1          # дубль webhook не породил второе SMS
    assert api.post("/api/v1/payments", json={"shipmentId": sid},
                    headers={"Idempotency-Key": str(uuid4())}).json()["code"] == "ALREADY_PAID"

    # Признак оплаты доезжает до постамата асинхронно
    wait_for(lambda: pickup(code), lambda r: r.status_code == 200, what="pickup after payment")
    wait_status(sid, "DELIVERED")


# ---------- TC-03: неуспешная оплата и повторная попытка ----------
def test_tc03_failed_payment_can_be_retried():
    sid, code = to_ready(pay_type="ON_PICKUP", amount=50000)
    p1 = api.post("/api/v1/payments", json={"shipmentId": sid}, headers={"Idempotency-Key": str(uuid4())}).json()
    httpx.post(f"{PSP}/psp/payments/{p1['paymentUrl'].rsplit('/', 1)[1]}/confirm", json={"outcome": "failed"})
    wait_for(lambda: api.get(f"/api/v1/payments/{p1['id']}").json(), lambda p: p["status"] == "FAILED")
    assert get(sid)["paymentStatus"] == "AWAITING"

    p2 = api.post("/api/v1/payments", json={"shipmentId": sid}, headers={"Idempotency-Key": str(uuid4())})
    assert p2.status_code == 201 and p2.json()["id"] != p1["id"]
    httpx.post(f"{PSP}/psp/payments/{p2.json()['paymentUrl'].rsplit('/', 1)[1]}/confirm", json={})
    wait_for(lambda: pickup(code), lambda r: r.status_code == 200, what="pickup after second payment")


# ---------- TC-04: нет свободных ячеек ----------
def test_tc04_no_free_cells_and_release_on_cancel():
    first = create(new_shipment(locker=MSK_002))
    wait_status(first["id"], "CELL_RESERVED")

    second = create(new_shipment(locker=MSK_002))
    rejected = wait_status(second["id"], "REJECTED")
    assert rejected["statusReason"] == "NO_FREE_CELLS"

    # Отмена первого отправления освобождает ячейку
    c = api.post(f"/api/v1/shipments/{first['id']}/cancel", json={"reason": "Покупатель передумал"}, headers=AUTH)
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
    wait_for(lambda: api.get(f"/api/v1/lockers/{MSK_002}").json(), lambda l: l["freeCells"]["S"] == 1,
             what="cell released")
    # Повторная отмена идемпотентна, закладка в отменённый резерв запрещена
    assert api.post(f"/api/v1/shipments/{first['id']}/cancel", headers=AUTH).status_code == 200
    assert deposit(first["id"], MSK_002, expect=409)["code"] == "INVALID_RESERVATION_STATE"


# ---------- TC-05: недоступный и несуществующий постамат ----------
@pytest.mark.parametrize("locker,reason", [(KZN_001, "LOCKER_UNAVAILABLE"), (str(uuid4()), "LOCKER_NOT_FOUND")])
def test_tc05_locker_problems(locker, reason):
    s = create(new_shipment(locker=locker))
    assert wait_status(s["id"], "REJECTED")["statusReason"] == reason


# ---------- TC-06: апгрейд размера ячейки ----------
def test_tc06_parcel_goes_to_bigger_cell_when_own_size_is_busy():
    lockers = api.get("/api/v1/lockers", params={"city": "Санкт-Петербург"}).json()["items"]
    free_l = next(l for l in lockers if l["id"] == SPB_001)["freeCells"]["L"]
    ids = [create(new_shipment(locker=SPB_001, size="L"))["id"] for _ in range(free_l)]
    for i in ids:
        wait_status(i, "CELL_RESERVED")
    extra = create(new_shipment(locker=SPB_001, size="L"))
    assert wait_status(extra["id"], "REJECTED")["statusReason"] == "NO_FREE_CELLS"   # L не помещается в M
    small = create(new_shipment(locker=SPB_001, size="S"))
    assert wait_status(small["id"], "CELL_RESERVED")["cellNumber"] <= 10            # S занимает S-ячейку
    for i in ids + [small["id"]]:
        api.post(f"/api/v1/shipments/{i}/cancel", headers=AUTH)


# ---------- TC-07: идемпотентность создания ----------
def test_tc07_create_idempotency():
    body, key = new_shipment(), str(uuid4())
    first = create(body, key)
    r = api.post("/api/v1/shipments", json=body, headers={**AUTH, "Idempotency-Key": key})
    assert r.status_code == 202 and r.json()["id"] == first["id"] and r.headers["Idempotent-Replayed"] == "true"

    changed = {**body, "parcelSize": "L"}
    assert create(changed, key, expect=422)["code"] == "IDEMPOTENCY_KEY_REUSED"
    dup = create(body, str(uuid4()), expect=409)
    assert dup["code"] == "DUPLICATE_EXTERNAL_ORDER" and dup["shipmentId"] == first["id"]
    assert api.get("/api/v1/shipments", params={"externalOrderId": body["externalOrderId"]},
                   headers=AUTH).json()["total"] == 1
    api.post(f"/api/v1/shipments/{first['id']}/cancel", headers=AUTH)


# ---------- TC-08: валидация и авторизация ----------
def test_tc08_validation_and_auth():
    assert api.post("/api/v1/shipments", json=new_shipment()).status_code == 401
    assert api.post("/api/v1/shipments", json=new_shipment(), headers={"X-API-Key": "wrong"}).status_code == 401
    assert api.post("/api/v1/shipments", json=new_shipment(), headers=AUTH).json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    bad_phone = new_shipment(recipient={"name": "Иван", "phone": "8-916-123"})
    err = create(bad_phone, expect=400)
    assert err["code"] == "VALIDATION_ERROR" and err["errors"][0]["field"] == "recipient.phone"
    assert create(new_shipment(pay_type="ON_PICKUP", amount=0), expect=400)["code"] == "VALIDATION_ERROR"
    assert create(new_shipment(pay_type="PREPAID", amount=100), expect=400)["code"] == "VALIDATION_ERROR"
    assert api.get(f"/api/v1/shipments/{uuid4()}", headers=AUTH).json()["code"] == "SHIPMENT_NOT_FOUND"


# ---------- TC-09: истечение срока хранения и возврат ----------
def test_tc09_storage_expiry_and_return():
    sid, code = to_ready(pay_type="ON_PICKUP", amount=30000)
    r = httpx.post(f"{SHIPMENT_INTERNAL}/internal/v1/shipments/{sid}/expire")
    assert r.status_code == 200 and r.json()["status"] == "EXPIRED"
    notifications(sid, "STORAGE_EXPIRED")

    # Код больше не действует, счёт закрыт, отмена невозможна
    wait_for(lambda: api.post("/api/v1/payments", json={"shipmentId": sid},
                              headers={"Idempotency-Key": str(uuid4())}).json().get("code"),
             lambda c: c == "INVOICE_CANCELLED", what="invoice cancelled")
    ret = lambda: api.post(f"/api/v1/lockers/{MSK_001}/returns", json={"shipmentId": sid, "courierId": "courier-7"})
    wait_for(ret, lambda r: r.status_code == 200, what="return allowed")
    assert pickup(code).status_code == 403
    assert api.post(f"/api/v1/shipments/{sid}/cancel", headers=AUTH).json()["code"] == "INVALID_SHIPMENT_STATE"
    wait_status(sid, "RETURNED")
    assert ret().status_code == 409

    _, good_code = to_ready()   # успешная выдача обнуляет счётчик неверных попыток постамата
    reset_pickup_block(MSK_001, good_code)


# ---------- TC-10: защита от подбора кода ----------
def test_tc10_pickup_code_bruteforce_protection():
    sid, code = to_ready(locker=SPB_001)
    wrong = "000000" if code != "000000" else "111111"
    for left in (4, 3, 2, 1, 0):
        r = pickup(wrong, SPB_001)
        assert r.status_code == 403 and r.json()["attemptsLeft"] == left
    blocked = pickup(code, SPB_001)                      # даже верный код не принимается во время блокировки
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    assert get(sid)["status"] == "READY_FOR_PICKUP"

    # Оператор поддержки снимает блокировку — получатель забирает посылку
    assert httpx.post(f"{LOCKER_INTERNAL}/internal/v1/lockers/{SPB_001}/unblock").status_code == 200
    assert pickup(code, SPB_001).status_code == 200
    wait_status(sid, "DELIVERED")


# ---------- TC-11: безопасность webhook ----------
def test_tc11_webhook_security():
    sid, code = to_ready(pay_type="ON_PICKUP", amount=77700)
    p = api.post("/api/v1/payments", json={"shipmentId": sid}, headers={"Idempotency-Key": str(uuid4())}).json()

    def hook(amount, secret=WEBHOOK_SECRET):
        raw = json.dumps({"eventId": f"evt_{uuid4().hex}", "pspPaymentId": p["paymentUrl"].rsplit("/", 1)[1],
                          "merchantPaymentId": p["id"], "status": "succeeded", "amount": amount,
                          "currency": "RUB"}).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return api.post("/api/v1/webhooks/psp", content=raw,
                        headers={"X-PSP-Signature": sig, "Content-Type": "application/json"})

    assert hook(77700, secret="attacker").json()["code"] == "INVALID_SIGNATURE"
    assert hook(1).json()["code"] == "AMOUNT_MISMATCH"
    assert get(sid)["paymentStatus"] == "AWAITING"
    assert hook(77700).json() == {"result": "ACCEPTED"}
    assert hook(77700).json() == {"result": "IGNORED"}   # новое уведомление по уже завершённому платежу
    wait_for(lambda: get(sid), lambda s: s["paymentStatus"] == "PAID", what="PAID")
    wait_for(lambda: pickup(code), lambda r: r.status_code == 200, what="pickup after payment")


# ---------- TC-12: сквозной correlation id ----------
def test_tc12_correlation_id_is_propagated():
    cid = f"e2e-{uuid4()}"
    r = api.get(f"/api/v1/shipments/{uuid4()}", headers={**AUTH, "X-Correlation-Id": cid})
    assert r.headers["X-Correlation-Id"] == cid and r.json()["correlationId"] == cid
