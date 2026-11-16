# Диаграммы последовательности

Показывают, **какой сервис что делает** в ключевых сценариях. Сплошная стрелка — синхронный вызов
(REST), пунктирная — асинхронное событие через Kafka. Имена событий и эндпоинтов совпадают
с [AsyncAPI](../05-integration/asyncapi/lockerflow-events.yaml) и [OpenAPI](../05-integration/openapi/).

## SD-01. Создание отправления и резервирование ячейки (сага)

Реализует UC-01. Тесты: TC-01, TC-04, TC-05.

```mermaid
sequenceDiagram
    autonumber
    actor P as Партнёр
    participant S as shipment-service
    participant K as Kafka
    participant L as locker-service
    participant Pay as payment-service

    P->>S: POST /api/v1/shipments<br/>X-API-Key, Idempotency-Key
    activate S
    S->>S: проверка ключа, валидация,<br/>проверка идемпотентности
    Note over S: Одна транзакция БД:<br/>INSERT shipment (CREATED)<br/>INSERT idempotency_key<br/>INSERT outbox (shipment.created)
    S-->>P: 202 Accepted {status: CREATED}
    deactivate S

    S-)K: shipment.created
    par Резервирование ячейки
        K-)L: shipment.created
        activate L
        L->>L: выбрать наименьшую подходящую<br/>свободную ячейку (FOR UPDATE SKIP LOCKED)
        alt ячейка найдена
            Note over L: Транзакция: ячейка RESERVED,<br/>резерв RESERVED, outbox
            L-)K: locker.cell.reserved
            K-)S: locker.cell.reserved
            S->>S: CREATED → CELL_RESERVED
        else нет ячеек / постамат недоступен
            L-)K: locker.cell.reservation_failed {reason}
            deactivate L
            K-)S: locker.cell.reservation_failed
            S->>S: CREATED → REJECTED
            S-)K: shipment.rejected
            K-)Pay: shipment.rejected
            Pay->>Pay: счёт → CANCELLED
        end
    and Открытие счёта (если ON_PICKUP)
        K-)Pay: shipment.created
        Pay->>Pay: INSERT invoice (OPEN)
    end

    P->>S: GET /api/v1/shipments/{id}
    S-->>P: 200 {status: CELL_RESERVED, cellNumber, lockerAddress}
```

**На что обратить внимание**

- Между шагами 2 и 3: запись данных и события — в одной транзакции (outbox). Событие не может «потеряться» между
  БД и Kafka.
- Компенсация саги при отказе в резерве — перевод отправления в `REJECTED` и закрытие счёта.
- Партнёр узнаёт итог опросом; развитие — webhook (US-16).

## SD-02. Закладка посылки и уведомление получателя

Реализует UC-02. Тесты: TC-01.

```mermaid
sequenceDiagram
    autonumber
    actor C as Курьер (терминал)
    participant L as locker-service
    participant K as Kafka
    participant S as shipment-service
    participant N as notification-service
    participant SMS as SMS-провайдер

    C->>L: POST /api/v1/lockers/{id}/deposits<br/>{shipmentId, courierId}
    activate L
    L->>L: найти резерв (RESERVED)<br/>сгенерировать код, вычислить HMAC
    Note over L: Транзакция: резерв DEPOSITED,<br/>ячейка OCCUPIED, outbox
    L-->>C: 201 {cellNumber, cellSize}
    deactivate L

    L-)K: locker.parcel.deposited {pickupCode}
    K-)S: locker.parcel.deposited
    S->>S: CELL_RESERVED → READY_FOR_PICKUP<br/>storage_until = now + 72 ч
    S-)K: shipment.ready_for_pickup<br/>{recipient, pickupCode, lockerAddress,<br/>storageUntil, paymentRequired, amount}
    K-)N: shipment.ready_for_pickup
    N->>N: сформировать текст по шаблону
    N->>SMS: отправить SMS
    N->>N: INSERT notification (SENT)
```

**На что обратить внимание**

- Код получения в открытом виде существует только в событии и в SMS; в БД locker-service — только
  HMAC. Компромисс разобран в [ADR-0008](adr/0008-pickup-code-in-event.md).
- `notification-service` не ходит за телефоном получателя в другой сервис: все данные для сообщения
  уже есть в событии (event-carried state transfer).

## SD-03. Оплата при получении

Реализует UC-03. Тесты: TC-02, TC-03, TC-11.

```mermaid
sequenceDiagram
    autonumber
    actor R as Получатель
    participant Pay as payment-service
    participant PSP as Платёжный шлюз
    participant K as Kafka
    participant S as shipment-service
    participant L as locker-service
    participant N as notification-service

    R->>Pay: POST /api/v1/payments {shipmentId}<br/>Idempotency-Key
    activate Pay
    Pay->>Pay: счёт OPEN? нет PENDING-платежа?<br/>(блокировка строки счёта)
    Pay->>PSP: POST /psp/payments<br/>{merchantPaymentId, amount, callbackUrl}
    PSP-->>Pay: 201 {pspPaymentId, paymentUrl}
    Pay->>Pay: INSERT payment (PENDING)
    Pay-->>R: 201 {paymentUrl}
    deactivate Pay

    R->>PSP: ввод данных карты, подтверждение
    PSP->>Pay: POST /api/v1/webhooks/psp<br/>X-PSP-Signature
    activate Pay
    Pay->>Pay: проверить подпись HMAC-SHA256
    Pay->>Pay: дедупликация по eventId,<br/>сверка суммы и валюты
    Note over Pay: Транзакция: платёж SUCCEEDED,<br/>счёт PAID, журнал webhook, outbox
    Pay-->>PSP: 200 {result: ACCEPTED}
    deactivate Pay

    Pay-)K: payment.succeeded
    K-)S: payment.succeeded
    S->>S: payment_status: AWAITING → PAID
    S-)K: shipment.paid
    par
        K-)L: shipment.paid
        L->>L: резерв: payment_required = false
    and
        K-)N: shipment.paid
        N->>N: SMS «Оплата получена»
    end
```

**На что обратить внимание**

- Шаг 3 — единственный синхронный межсистемный вызов. При таймауте PSP получатель видит
  `502 PSP_UNAVAILABLE`, платёж не создаётся, повтор безопасен: PSP идемпотентен по `merchantPaymentId`.
- Шаги 8–11: подпись проверяется **до** разбора тела; дубль уведомления даёт `200` без действий —
  иначе PSP продолжит повторы.
- Между шагом 11 (PSP получил подтверждение) и шагом 17 (постамат узнал об оплате) проходит ненулевое время: получатель, мгновенно введший код, получит
  `409 PAYMENT_REQUIRED`. Целевая задержка — NFR-04 (p95 ≤ 5 с); терминал повторяет запрос.

## SD-04. Выдача посылки

Реализует UC-04. Тесты: TC-01, TC-02, TC-10.

```mermaid
sequenceDiagram
    autonumber
    actor R as Получатель (терминал)
    participant L as locker-service
    participant K as Kafka
    participant S as shipment-service
    participant N as notification-service

    R->>L: POST /api/v1/lockers/{id}/pickups {pickupCode}
    activate L
    L->>L: блокировка строки постамата
    alt ввод заблокирован
        L-->>R: 429 PICKUP_BLOCKED, Retry-After
    else код не найден
        L->>L: failed_attempts + 1<br/>(на 5-й — blocked_until = now + 10 мин)
        L-->>R: 403 INVALID_PICKUP_CODE {attemptsLeft}
    else найден, но не оплачен
        L-->>R: 409 PAYMENT_REQUIRED {shipmentId}
    else найден и разрешён к выдаче
        Note over L: Транзакция: резерв PICKED_UP,<br/>ячейка FREE, счётчик = 0, outbox
        L-->>R: 200 {cellNumber}
    end
    deactivate L

    L-)K: locker.parcel.picked_up
    K-)S: locker.parcel.picked_up
    S->>S: READY_FOR_PICKUP → DELIVERED
    S-)K: shipment.delivered
    K-)N: shipment.delivered
    N->>N: SMS «Заказ получен»
```

**На что обратить внимание**

- Решение «открывать или нет» принимается **только по локальным данным** locker-service — терминал
  работает, даже если shipment- и payment-сервисы недоступны (NFR-09).
- Блокировка строки постамата сериализует ввод кодов на одном терминале: счётчик попыток нельзя
  обойти параллельными запросами.

## SD-05. Истечение срока хранения и возврат

Реализует UC-06. Тесты: TC-09.

```mermaid
sequenceDiagram
    autonumber
    participant T as Таймер (storage_expiry_job)
    participant S as shipment-service
    participant K as Kafka
    participant L as locker-service
    participant Pay as payment-service
    participant N as notification-service
    actor C as Курьер (терминал)

    loop каждые 5 секунд
        T->>S: выбрать READY_FOR_PICKUP,<br/>где storage_until < now()<br/>(FOR UPDATE SKIP LOCKED)
    end
    S->>S: READY_FOR_PICKUP → EXPIRED
    S-)K: shipment.expired
    par
        K-)L: shipment.expired
        L->>L: резерв: DEPOSITED → STORAGE_EXPIRED<br/>(код больше не действует)
    and
        K-)Pay: shipment.expired
        Pay->>Pay: счёт OPEN → CANCELLED
    and
        K-)N: shipment.expired
        N->>N: SMS «Срок хранения истёк»
    end

    C->>L: POST /api/v1/lockers/{id}/returns {shipmentId, courierId}
    L->>L: резерв RETURNED, ячейка FREE
    L-->>C: 200 {cellNumber}
    L-)K: locker.parcel.returned
    K-)S: locker.parcel.returned
    S->>S: EXPIRED → RETURNED
    S-)K: shipment.returned
```

## SD-06. Отмена во время резервирования (гонка)

Реализует UC-05, альтернативный поток 4а. Иллюстрирует, почему важен порядок событий.

```mermaid
sequenceDiagram
    autonumber
    actor P as Партнёр
    participant S as shipment-service
    participant K as Kafka (партиция по shipmentId)
    participant L as locker-service

    P->>S: POST /shipments
    S-)K: ① shipment.created
    P->>S: POST /shipments/{id}/cancel
    S->>S: CREATED → CANCELLED
    S-)K: ② shipment.cancelled
    S-->>P: 200 {status: CANCELLED}

    K-)L: ① shipment.created
    L->>L: зарезервировать ячейку
    L-)K: locker.cell.reserved
    K-)L: ② shipment.cancelled
    L->>L: освободить ячейку
    L-)K: locker.cell.released

    K-)S: locker.cell.reserved
    S->>S: переход CANCELLED → CELL_RESERVED<br/>запрещён — событие игнорируется
```

**Вывод:** корректность обеспечивают два свойства — (1) события одного отправления лежат в одной
партиции и читаются по порядку, (2) каждый сервис проверяет допустимость перехода по своей машине
состояний. Ячейка не «зависает» в резерве, хотя отмена обогнала резервирование.

## SD-07. Надёжная доставка события (outbox и идемпотентный потребитель)

Технический сценарий, общий для всех сервисов. Обоснование — [ADR-0005](adr/0005-outbox-and-idempotent-consumer.md).

```mermaid
sequenceDiagram
    autonumber
    participant A as Сервис-издатель
    participant DBA as БД издателя
    participant R as Outbox Relay
    participant K as Kafka
    participant B as Сервис-потребитель
    participant DBB as БД потребителя

    A->>DBA: BEGIN
    A->>DBA: UPDATE бизнес-данные
    A->>DBA: INSERT INTO outbox
    A->>DBA: COMMIT
    loop каждые 200 мс
        R->>DBA: SELECT неопубликованные<br/>FOR UPDATE SKIP LOCKED
        R->>K: send (acks=all)
        R->>DBA: UPDATE published_at
    end
    Note over R,K: сбой между send и UPDATE →<br/>событие будет отправлено повторно

    K-)B: событие (возможно, повторно)
    B->>DBB: BEGIN
    B->>DBB: INSERT INTO processed_events<br/>ON CONFLICT DO NOTHING
    alt eventId новый
        B->>DBB: бизнес-обработка (+ свой outbox)
        B->>DBB: COMMIT
    else eventId уже обработан
        B->>DBB: ROLLBACK (пропустить)
    end
    B->>K: commit offset
    Note over B,K: 3 неудачные попытки →<br/>событие уходит в lockerflow.dlq
```
