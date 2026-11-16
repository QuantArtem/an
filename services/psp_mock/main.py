"""psp-mock — эмулятор внешнего платёжного шлюза (PSP) для стенда и e2e-тестов.

Ведёт себя как настоящий PSP: создаёт платёж, отдаёт ссылку на платёжную страницу,
а после «оплаты» шлёт подписанный webhook в payment-service.
"""
import hashlib
import hmac
import json
import os
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

SECRET = os.environ.get("PSP_WEBHOOK_SECRET", "dev-webhook-secret")
PUBLIC_URL = os.environ.get("PSP_PUBLIC_URL", "http://localhost:8010")

app = FastAPI(title="PSP Mock", version="1.0.0")
PAYMENTS: dict[str, dict] = {}


class CreatePayment(BaseModel):
    merchantPaymentId: str
    amount: int
    currency: str
    callbackUrl: str
    description: str | None = None


class Confirm(BaseModel):
    outcome: Literal["succeeded", "failed"] = "succeeded"
    duplicate: bool = False  # отправить уведомление дважды — проверка идемпотентности получателя


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "UP", "service": "psp-mock"}


@app.post("/psp/payments", status_code=201)
async def create(body: CreatePayment):
    # Идемпотентность по merchantPaymentId, как у реальных шлюзов
    for p in PAYMENTS.values():
        if p["merchantPaymentId"] == body.merchantPaymentId:
            return {"pspPaymentId": p["id"], "paymentUrl": f"{PUBLIC_URL}/psp/pay/{p['id']}"}
    pid = f"psp_{uuid4().hex[:16]}"
    PAYMENTS[pid] = {"id": pid, **body.model_dump(), "status": "pending"}
    return {"pspPaymentId": pid, "paymentUrl": f"{PUBLIC_URL}/psp/pay/{pid}"}


@app.post("/psp/payments/{pid}/confirm")
async def confirm(pid: str, body: Confirm | None = None):
    """Имитация действия плательщика на платёжной странице."""
    body = body or Confirm()
    p = PAYMENTS.get(pid)
    if not p:
        raise HTTPException(404, "payment not found")
    p["status"] = body.outcome
    raw = json.dumps({"eventId": f"evt_{uuid4().hex[:16]}", "pspPaymentId": pid,
                      "merchantPaymentId": p["merchantPaymentId"], "status": body.outcome,
                      "amount": p["amount"], "currency": p["currency"]}).encode()
    headers = {"Content-Type": "application/json",
               "X-PSP-Signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()}
    async with httpx.AsyncClient(timeout=5.0) as client:
        codes = [(await client.post(p["callbackUrl"], content=raw, headers=headers)).status_code
                 for _ in range(2 if body.duplicate else 1)]
    return {"pspPaymentId": pid, "status": body.outcome, "webhookStatusCodes": codes}


@app.get("/psp/pay/{pid}", response_class=HTMLResponse)
async def pay_page(pid: str):
    p = PAYMENTS.get(pid)
    if not p:
        raise HTTPException(404, "payment not found")
    return f"""<!doctype html><meta charset="utf-8"><title>PSP Mock</title>
<body style="font-family:sans-serif;max-width:420px;margin:60px auto">
<h2>Тестовая платёжная страница</h2><p>{p.get('description') or ''}</p>
<p>Сумма: <b>{p['amount'] / 100:.2f} {p['currency']}</b> · статус: <b id="s">{p['status']}</b></p>
<button onclick="go('succeeded')">Оплатить</button> <button onclick="go('failed')">Отклонить</button>
<script>async function go(o){{const r=await fetch('/psp/payments/{pid}/confirm',{{method:'POST',
headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{outcome:o}})}});
document.getElementById('s').textContent=(await r.json()).status}}</script></body>"""
