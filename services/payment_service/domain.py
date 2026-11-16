"""Доменные правила платежей — чистые функции, покрыты unit-тестами."""
import hashlib
import hmac


def sign(secret: str, raw_body: bytes) -> str:
    """Подпись уведомления PSP: hex(HMAC-SHA256(secret, тело запроса))."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, raw_body: bytes, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(sign(secret, raw_body), signature)


def webhook_outcome(payment_status: str, webhook_status: str) -> str:
    """Что делать с уведомлением: APPLY_SUCCESS | APPLY_FAILURE | IGNORE.

    Финальный статус платежа не меняется (BR-11): повторные и запоздавшие уведомления игнорируются.
    """
    if payment_status != "PENDING":
        return "IGNORE"
    return "APPLY_SUCCESS" if webhook_status == "succeeded" else "APPLY_FAILURE"
