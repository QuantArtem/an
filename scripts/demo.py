#!/usr/bin/env python3
"""Демонстрация сквозного сценария «оплата при получении» на запущенном стенде.

Запуск:  python3 scripts/demo.py        (нужен только Python 3, без зависимостей)
"""
import json
import re
import time
import urllib.error
import urllib.request
import uuid

GW = "http://localhost:8080"
PSP = "http://localhost:8010"
LOCKER = "11111111-1111-4111-8111-111111111111"
AUTH = {"X-API-Key": "demo-partner-key"}


def call(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait(fn, ok, what):
    for _ in range(100):
        value = fn()
        if ok(value):
            return value
        time.sleep(0.2)
    raise SystemExit(f"не дождались: {what}")


def step(n, title):
    print(f"\n\033[1m[{n}] {title}\033[0m")


def shipment(sid):
    return call("GET", f"{GW}/api/v1/shipments/{sid}", headers=AUTH)[1]


step(1, "Магазин смотрит свободные ячейки в постаматах Москвы")
for l in call("GET", f"{GW}/api/v1/lockers?city=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0")[1]["items"]:
    print(f"    {l['code']}  {l['address']:<32} свободно: {l['freeCells']}")

step(2, "Магазин создаёт отправление с оплатой при получении (1 499 ₽)")
code, s = call("POST", f"{GW}/api/v1/shipments", {
    "externalOrderId": f"DEMO-{uuid.uuid4().hex[:8]}", "lockerId": LOCKER, "parcelSize": "M",
    "recipient": {"name": "Анна Смирнова", "phone": "+79031112233"},
    "payment": {"type": "ON_PICKUP", "amount": 149900}}, {**AUTH, "Idempotency-Key": str(uuid.uuid4())})
sid = s["id"]
print(f"    HTTP {code}: отправление {sid}, статус {s['status']}")

step(3, "Сага: locker-service резервирует ячейку по событию shipment.created")
s = wait(lambda: shipment(sid), lambda x: x["status"] == "CELL_RESERVED", "резерв ячейки")
print(f"    статус {s['status']}, ячейка №{s['cellNumber']}, {s['lockerAddress']}")

step(4, "Курьер закладывает посылку")
code, d = call("POST", f"{GW}/api/v1/lockers/{LOCKER}/deposits", {"shipmentId": sid, "courierId": "courier-7"})
print(f"    HTTP {code}: открыта ячейка №{d['cellNumber']} (размер {d['cellSize']})")
s = wait(lambda: shipment(sid), lambda x: x["status"] == "READY_FOR_PICKUP", "готовность к выдаче")
print(f"    статус {s['status']}, хранение до {s['storageUntil']}")

step(5, "Получателю приходит SMS с кодом")
sms = wait(lambda: call("GET", f"{GW}/api/v1/notifications?shipmentId={sid}")[1]["items"], bool, "SMS")[0]
print(f"    SMS на {sms['recipient']}: «{sms['body']}»")
pickup_code = re.search(r"Код получения: (\d{6})", sms["body"]).group(1)

step(6, "Получатель пробует открыть ячейку без оплаты")
code, e = call("POST", f"{GW}/api/v1/lockers/{LOCKER}/pickups", {"pickupCode": pickup_code})
print(f"    HTTP {code}: {e['code']} — {e['detail']}")

step(7, "Получатель создаёт платёж и оплачивает на странице платёжного шлюза")
code, p = call("POST", f"{GW}/api/v1/payments", {"shipmentId": sid}, {"Idempotency-Key": str(uuid.uuid4())})
print(f"    HTTP {code}: платёж {p['id']}, статус {p['status']}\n    страница оплаты: {p['paymentUrl']}")
_, c = call("POST", f"{PSP}/psp/payments/{p['paymentUrl'].rsplit('/', 1)[1]}/confirm", {"outcome": "succeeded"})
print(f"    PSP отправил подписанный webhook, ответ payment-service: {c['webhookStatusCodes']}")
s = wait(lambda: shipment(sid), lambda x: x["paymentStatus"] == "PAID", "оплата")
print(f"    статус оплаты отправления: {s['paymentStatus']}")

step(8, "Получатель вводит код — ячейка открывается")
code, r = wait(lambda: call("POST", f"{GW}/api/v1/lockers/{LOCKER}/pickups", {"pickupCode": pickup_code}),
               lambda x: x[0] == 200, "выдача")
print(f"    HTTP {code}: открыта ячейка №{r['cellNumber']}")
s = wait(lambda: shipment(sid), lambda x: x["status"] == "DELIVERED", "доставка")

step(9, "Итог: история статусов и уведомления")
for h in call("GET", f"{GW}/api/v1/shipments/{sid}/history", headers=AUTH)[1]["items"]:
    print(f"    {h['createdAt'][11:19]}  {h['fromStatus'] or '—':<17} → {h['toStatus']:<17} ({h['actor']})")
time.sleep(1)
for n in call("GET", f"{GW}/api/v1/notifications?shipmentId={sid}")[1]["items"]:
    print(f"    SMS [{n['template']}]")
print("\nГотово. Логи с цепочкой событий:  docker compose logs | grep 'event handled'")
