-- БД платёжного сервиса (payment_db). Владелец данных: payment-service.
CREATE TABLE IF NOT EXISTS invoices (                  -- счёт к оплате: проекция события shipment.created
    shipment_id UUID PRIMARY KEY,
    amount      BIGINT      NOT NULL CHECK (amount > 0),   -- в копейках
    currency    TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'PAID', 'CANCELLED')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payments (                  -- попытка оплаты счёта
    id              UUID PRIMARY KEY,
    shipment_id     UUID        NOT NULL REFERENCES invoices (shipment_id),
    amount          BIGINT      NOT NULL,
    currency        TEXT        NOT NULL,
    status          TEXT        NOT NULL CHECK (status IN ('PENDING', 'SUCCEEDED', 'FAILED')),
    psp_payment_id  TEXT        NOT NULL UNIQUE,
    payment_url     TEXT        NOT NULL,
    idempotency_key TEXT        NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payments_shipment_idx ON payments (shipment_id);

CREATE TABLE IF NOT EXISTS psp_webhook_log (           -- журнал входящих уведомлений PSP (аудит + дедупликация)
    psp_event_id TEXT PRIMARY KEY,
    payment_id   UUID,
    payload      JSONB       NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
