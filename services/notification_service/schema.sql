-- БД сервиса уведомлений (notification_db). Владелец данных: notification-service.
CREATE TABLE IF NOT EXISTS notifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shipment_id UUID        NOT NULL,
    event_id    UUID        NOT NULL,                  -- событие-причина
    channel     TEXT        NOT NULL CHECK (channel IN ('SMS', 'EMAIL')),
    recipient   TEXT        NOT NULL,
    template    TEXT        NOT NULL,
    body        TEXT        NOT NULL,
    status      TEXT        NOT NULL CHECK (status IN ('SENT', 'FAILED')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, channel)                         -- одно событие — одно сообщение в канал
);
CREATE INDEX IF NOT EXISTS notifications_shipment_idx ON notifications (shipment_id, created_at);
