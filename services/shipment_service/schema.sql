-- БД сервиса отправлений (shipment_db). Владелец данных: shipment-service.
CREATE TABLE IF NOT EXISTS partners (
    id           UUID PRIMARY KEY,
    name         TEXT        NOT NULL,
    api_key_hash TEXT        NOT NULL UNIQUE,          -- SHA-256 от API-ключа, сам ключ не хранится
    status       TEXT        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'BLOCKED')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shipments (
    id                UUID PRIMARY KEY,
    partner_id        UUID        NOT NULL REFERENCES partners (id),
    external_order_id TEXT        NOT NULL,            -- номер заказа в системе магазина
    locker_id         UUID        NOT NULL,            -- ссылка на агрегат другого сервиса (без FK)
    locker_address    TEXT,                            -- денормализация из события locker.cell.reserved
    cell_number       INT,
    parcel_size       TEXT        NOT NULL CHECK (parcel_size IN ('S', 'M', 'L')),
    recipient_name    TEXT        NOT NULL,
    recipient_phone   TEXT        NOT NULL,
    recipient_email   TEXT,
    payment_type      TEXT        NOT NULL CHECK (payment_type IN ('PREPAID', 'ON_PICKUP')),
    amount            BIGINT      NOT NULL DEFAULT 0 CHECK (amount >= 0),   -- в копейках
    currency          TEXT        NOT NULL DEFAULT 'RUB',
    status            TEXT        NOT NULL CHECK (status IN ('CREATED', 'CELL_RESERVED', 'READY_FOR_PICKUP',
                                  'DELIVERED', 'REJECTED', 'CANCELLED', 'EXPIRED', 'RETURNED')),
    payment_status    TEXT        NOT NULL CHECK (payment_status IN ('NOT_REQUIRED', 'AWAITING', 'PAID')),
    status_reason     TEXT,
    storage_until     TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (partner_id, external_order_id)
);
CREATE INDEX IF NOT EXISTS shipments_expiry_idx ON shipments (storage_until) WHERE status = 'READY_FOR_PICKUP';
CREATE INDEX IF NOT EXISTS shipments_partner_status_idx ON shipments (partner_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS shipment_status_history (
    id          BIGSERIAL PRIMARY KEY,
    shipment_id UUID        NOT NULL REFERENCES shipments (id),
    from_status TEXT,
    to_status   TEXT        NOT NULL,
    reason      TEXT,
    actor       TEXT        NOT NULL,                  -- partner | system | operator | locker-service ...
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ssh_shipment_idx ON shipment_status_history (shipment_id, id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    partner_id   UUID        NOT NULL REFERENCES partners (id),
    idem_key     TEXT        NOT NULL,
    request_hash TEXT        NOT NULL,
    shipment_id  UUID        NOT NULL REFERENCES shipments (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (partner_id, idem_key)
);

-- Демо-партнёр. API-ключ: demo-partner-key
INSERT INTO partners (id, name, api_key_hash)
VALUES ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'Демо-магазин «Книжный червь»',
        encode(sha256('demo-partner-key'::bytea), 'hex'))
ON CONFLICT DO NOTHING;
