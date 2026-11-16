from payment_service.domain import sign, verify_signature, webhook_outcome


def test_signature_roundtrip():
    body = b'{"eventId":"evt_1"}'
    assert verify_signature("secret", body, sign("secret", body))


def test_signature_rejects_tampering():
    sig = sign("secret", b'{"amount":100}')
    assert not verify_signature("secret", b'{"amount":999}', sig)
    assert not verify_signature("other-secret", b'{"amount":100}', sig)
    assert not verify_signature("secret", b'{"amount":100}', None)


def test_webhook_outcome():
    """BR-11: финальный статус платежа неизменяем."""
    assert webhook_outcome("PENDING", "succeeded") == "APPLY_SUCCESS"
    assert webhook_outcome("PENDING", "failed") == "APPLY_FAILURE"
    assert webhook_outcome("SUCCEEDED", "failed") == "IGNORE"
    assert webhook_outcome("FAILED", "succeeded") == "IGNORE"
