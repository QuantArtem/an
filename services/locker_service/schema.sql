-- БД сервиса постаматов (locker_db). Владелец данных: locker-service.
CREATE TABLE IF NOT EXISTS lockers (
    id              UUID PRIMARY KEY,
    code            TEXT        NOT NULL UNIQUE,       -- человекочитаемый код: MSK-001
    city            TEXT        NOT NULL,
    address         TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'OFFLINE', 'MAINTENANCE')),
    failed_attempts INT         NOT NULL DEFAULT 0,    -- подряд идущие неверные вводы кода (BR-08)
    blocked_until   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cells (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    locker_id UUID NOT NULL REFERENCES lockers (id),
    number    INT  NOT NULL,
    size      TEXT NOT NULL CHECK (size IN ('S', 'M', 'L')),
    status    TEXT NOT NULL DEFAULT 'FREE' CHECK (status IN ('FREE', 'RESERVED', 'OCCUPIED', 'OUT_OF_SERVICE')),
    UNIQUE (locker_id, number)
);
CREATE INDEX IF NOT EXISTS cells_free_idx ON cells (locker_id, size) WHERE status = 'FREE';

CREATE TABLE IF NOT EXISTS reservations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shipment_id      UUID        NOT NULL UNIQUE,      -- одно отправление = один резерв
    locker_id        UUID        NOT NULL REFERENCES lockers (id),
    cell_id          UUID        NOT NULL REFERENCES cells (id),
    status           TEXT        NOT NULL CHECK (status IN ('RESERVED', 'DEPOSITED', 'PICKED_UP', 'RELEASED',
                                 'STORAGE_EXPIRED', 'RETURNED')),
    payment_required BOOLEAN     NOT NULL DEFAULT FALSE, -- локальная проекция статуса оплаты
    pickup_code_hash TEXT,                               -- HMAC-SHA256, сам код не хранится
    courier_id       TEXT,
    expires_at       TIMESTAMPTZ NOT NULL,               -- до какого момента держим резерв без закладки
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS reservations_code_idx ON reservations (locker_id, pickup_code_hash)
    WHERE status = 'DEPOSITED';
CREATE INDEX IF NOT EXISTS reservations_ttl_idx ON reservations (expires_at) WHERE status = 'RESERVED';

-- Демо-данные
INSERT INTO lockers (id, code, city, address, status) VALUES
    ('11111111-1111-4111-8111-111111111111', 'MSK-001', 'Москва', 'Москва, ул. Тверская, 7', 'ACTIVE'),
    ('22222222-2222-4222-8222-222222222222', 'SPB-001', 'Санкт-Петербург', 'Санкт-Петербург, Невский пр., 28', 'ACTIVE'),
    ('33333333-3333-4333-8333-333333333333', 'MSK-002', 'Москва', 'Москва, ул. Профсоюзная, 61', 'ACTIVE'),
    ('44444444-4444-4444-8444-444444444444', 'KZN-001', 'Казань', 'Казань, ул. Баумана, 15', 'OFFLINE')
ON CONFLICT DO NOTHING;

INSERT INTO cells (locker_id, number, size)
SELECT l.id, n, CASE WHEN n <= 10 THEN 'S' WHEN n <= 16 THEN 'M' ELSE 'L' END
FROM lockers l, generate_series(1, 20) n
WHERE l.code IN ('MSK-001', 'SPB-001', 'KZN-001')
ON CONFLICT DO NOTHING;

-- MSK-002: единственная ячейка — удобно демонстрировать сценарий «нет свободных ячеек»
INSERT INTO cells (locker_id, number, size)
VALUES ('33333333-3333-4333-8333-333333333333', 1, 'S')
ON CONFLICT DO NOTHING;
