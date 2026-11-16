# Каталог интеграций

Сводное описание всех взаимодействий системы. Детальные контракты:

- REST — [openapi/](openapi/) (по файлу на сервис);
- события — [asyncapi/lockerflow-events.yaml](asyncapi/lockerflow-events.yaml);
- ошибки — [error-catalog.md](error-catalog.md).

## Реестр интеграционных потоков

| ID | Источник | Приёмник | Способ | Режим | Назначение | Контракт |
|----|----------|----------|--------|-------|------------|----------|
| INT-01 | Интернет-магазин | shipment-service | REST/JSON | Синхр. | Создание, отмена, получение статуса отправлений | `shipment-service.yaml` |
| INT-02 | Интернет-магазин | locker-service | REST/JSON | Синхр. | Справочник постаматов и свободных ячеек | `locker-service.yaml` |
| INT-03 | Терминал постамата | locker-service | REST/JSON | Синхр. | Закладка, выдача, изъятие | `locker-service.yaml` |
| INT-04 | Страница оплаты | payment-service | REST/JSON | Синхр. | Создание платежа, получение его статуса | `payment-service.yaml` |
| INT-05 | payment-service | PSP | REST/JSON | Синхр. | Регистрация платежа в шлюзе | [ниже](#int-05-создание-платежа-в-psp) |
| INT-06 | PSP | payment-service | Webhook | Асинхр. | Результат платежа | `payment-service.yaml`, [ниже](#int-06-webhook-psp) |
| INT-07 | notification-service | SMS-провайдер | REST/JSON | Синхр. | Отправка SMS | [ниже](#int-07-sms-провайдер) |
| INT-08 | shipment-service | locker, payment, notification | Kafka `shipment.events` | Асинхр. | События жизненного цикла отправления | AsyncAPI |
| INT-09 | locker-service | shipment-service | Kafka `locker.events` | Асинхр. | События ячеек и операций терминала | AsyncAPI |
| INT-10 | payment-service | shipment-service | Kafka `payment.events` | Асинхр. | Результаты оплаты | AsyncAPI |
| INT-11 | Оператор | shipment, locker | REST/JSON (`/internal`) | Синхр. | Операторские действия | OpenAPI, тег `internal` |

## Общие соглашения REST

| Аспект | Соглашение |
|--------|------------|
| Версионирование | Мажорная версия в пути: `/api/v1`. Добавление необязательных полей и новых эндпоинтов версию не меняет. При выпуске `v2` версия `v1` поддерживается не менее 6 месяцев |
| Именование | Ресурсы — существительные во множественном числе; поля JSON — `camelCase`; действия, не укладывающиеся в CRUD, — подресурс-глагол (`/cancel`) |
| Идентификаторы | UUID v4, генерируются сервером |
| Деньги | Целое число в копейках (`amount: 149900` = 1 499,00 ₽) + код валюты |
| Время | ISO 8601 в UTC: `2026-03-01T10:30:00+00:00` |
| Пагинация | `limit` (1–100, по умолчанию 20) и `offset`; ответ содержит `total` |
| Идемпотентность | Заголовок `Idempotency-Key` обязателен для `POST`, создающих ресурсы (ADR-0006) |
| Трассировка | `X-Correlation-Id`: принимается от клиента или генерируется, возвращается в ответе и в теле ошибки |
| Ошибки | RFC 7807, машиночитаемое поле `code` |
| Неизвестные поля | Отклоняются (`400`): опечатка в имени поля не должна молча игнорироваться |
| Коды ответов | `200` — выполнено; `201` — создано; `202` — принято, результат позже; `4xx` — ошибка клиента; `5xx` — ошибка системы |

## Маршрутизация API Gateway

| Префикс пути | Сервис |
|--------------|--------|
| `/api/v1/shipments`, `/api/v1/tracking` | shipment-service |
| `/api/v1/lockers` | locker-service |
| `/api/v1/payments`, `/api/v1/webhooks` | payment-service |
| `/api/v1/notifications` | notification-service |
| `/internal/*` | не публикуется (`404`) |

Конфигурация — `deploy/nginx.conf`.

---

## INT-05. Создание платежа в PSP

| Параметр | Значение |
|----------|----------|
| Направление | payment-service → PSP |
| Протокол | HTTPS, REST/JSON |
| Аутентификация | Целевая: API-ключ мерчанта в заголовке `Authorization`. На стенде — без аутентификации (psp-mock) |
| Таймаут | 5 секунд на запрос |
| Повторы | Автоматических нет: клиенту возвращается `502 PSP_UNAVAILABLE`, повтор инициирует получатель |
| Идемпотентность | По `merchantPaymentId`: повторный запрос с тем же идентификатором возвращает тот же платёж |
| Поведение при отказе | Платёж в LockerFlow не создаётся; счёт остаётся открытым |

**Запрос** `POST {PSP_URL}/psp/payments`

| Поле | Тип | Обяз. | Источник | Описание |
|------|-----|:-----:|----------|----------|
| `merchantPaymentId` | uuid | ✔ | `payments.id` | Идентификатор платежа в LockerFlow |
| `amount` | integer | ✔ | `invoices.amount` | Сумма в копейках |
| `currency` | string | ✔ | `invoices.currency` | `RUB` |
| `callbackUrl` | uri | ✔ | конфигурация `PAYMENT_WEBHOOK_URL` | Адрес для уведомления о результате |
| `description` | string | | формируется | Назначение платежа для выписки |

**Ответ** `201`

| Поле | Тип | Куда сохраняется | Описание |
|------|-----|------------------|----------|
| `pspPaymentId` | string | `payments.psp_payment_id` | Идентификатор платежа в PSP |
| `paymentUrl` | uri | `payments.payment_url` | Платёжная страница для получателя |

## INT-06. Webhook PSP

| Параметр | Значение |
|----------|----------|
| Направление | PSP → payment-service, `POST /api/v1/webhooks/psp` |
| Аутентификация | Заголовок `X-PSP-Signature` = hex(HMAC-SHA256(общий секрет, сырое тело)). Целевое дополнение — список разрешённых IP на шлюзе |
| Гарантия доставки со стороны PSP | At-least-once: повторы до получения `2xx` |
| Дедупликация | По `eventId` (таблица `psp_webhook_log`) |
| Порядок | Не гарантируется; защищает правило «финальный статус не меняется» (BR-11) |
| Требование ко времени ответа | ≤ 3 секунд, иначе PSP считает доставку неуспешной |

Маппинг полей уведомления:

| Поле уведомления | Проверка / использование |
|------------------|--------------------------|
| `eventId` | Ключ дедупликации |
| `merchantPaymentId` | Поиск платежа: `payments.id` |
| `pspPaymentId` | Сверка с `payments.psp_payment_id`; несовпадение → `404` |
| `amount`, `currency` | Сверка с платежом; несовпадение → `422 AMOUNT_MISMATCH` |
| `status = succeeded` | Платёж `SUCCEEDED`, счёт `PAID`, событие `payment.succeeded` |
| `status = failed` | Платёж `FAILED`, событие `payment.failed` |

Таблица решений обработки:

| Подпись | `eventId` | Платёж найден | Сумма совпала | Статус платежа | Результат |
|:-------:|:---------:|:-------------:|:-------------:|:--------------:|-----------|
| ✗ | — | — | — | — | `401 INVALID_SIGNATURE` |
| ✓ | уже был | — | — | — | `200 DUPLICATE` |
| ✓ | новый | ✗ | — | — | `404 PAYMENT_NOT_FOUND` |
| ✓ | новый | ✓ | ✗ | — | `422 AMOUNT_MISMATCH` |
| ✓ | новый | ✓ | ✓ | финальный | `200 IGNORED` |
| ✓ | новый | ✓ | ✓ | `PENDING` | `200 ACCEPTED`, статус изменён |

## INT-07. SMS-провайдер

| Параметр | Значение |
|----------|----------|
| Направление | notification-service → SMS-провайдер |
| Состояние | **В прототипе — заглушка** (`send_sms` пишет сообщение в лог). Ниже — требования к промышленной реализации |
| Протокол | HTTPS, REST/JSON |
| Таймаут | 3 секунды |
| Повторы | 3 попытки с паузами 1, 5, 30 с; затем уведомление получает статус `FAILED` |
| Идемпотентность | Идентификатор сообщения = `notifications.id` |
| Статус доставки | Целевое: приём отчётов о доставке (DLR) webhook'ом, статус `DELIVERED` / `UNDELIVERED` |
| Резервирование | Целевое: второй провайдер, переключение при доле ошибок > 5 % за 5 минут |

Шаблоны сообщений (`services/notification_service/templates.py`):

| Шаблон | Событие | Текст | Переменные |
|--------|---------|-------|------------|
| `READY_FOR_PICKUP` | `shipment.ready_for_pickup` | «Заказ {externalOrderId} ждёт вас в постамате: {lockerAddress}. Код получения: {pickupCode}. Хранение до {storageUntil} (UTC).» + при оплате: «К оплате {amount}: {ссылка}» | из полезной нагрузки события |
| `PAYMENT_RECEIVED` | `shipment.paid` | «Оплата {amount} по заказу {externalOrderId} получена. Введите код получения на экране постамата.» | |
| `DELIVERED` | `shipment.delivered` | «Заказ {externalOrderId} получен. Спасибо, что пользуетесь LockerFlow!» | |
| `STORAGE_EXPIRED` | `shipment.expired` | «Срок хранения заказа {externalOrderId} истёк. Заказ будет возвращён в магазин.» | |

---

## События: кто публикует и кто подписан

| Событие | Издатель | locker | shipment | payment | notification |
|---------|----------|:------:|:--------:|:-------:|:------------:|
| `shipment.created` | shipment | ✔ резерв | | ✔ счёт | |
| `shipment.rejected` | shipment | | | ✔ закрыть счёт | |
| `shipment.cancelled` | shipment | ✔ освободить | | ✔ закрыть счёт | |
| `shipment.ready_for_pickup` | shipment | | | | ✔ SMS с кодом |
| `shipment.paid` | shipment | ✔ разрешить выдачу | | | ✔ SMS |
| `shipment.delivered` | shipment | | | | ✔ SMS |
| `shipment.expired` | shipment | ✔ аннулировать код | | ✔ закрыть счёт | ✔ SMS |
| `shipment.returned` | shipment | | | | |
| `locker.cell.reserved` | locker | | ✔ | | |
| `locker.cell.reservation_failed` | locker | | ✔ | | |
| `locker.cell.released` | locker | | | | |
| `locker.reservation.expired` | locker | | ✔ | | |
| `locker.parcel.deposited` | locker | | ✔ | | |
| `locker.parcel.picked_up` | locker | | ✔ | | |
| `locker.parcel.returned` | locker | | ✔ | | |
| `payment.succeeded` | payment | | ✔ | | |
| `payment.failed` | payment | | | | |

События без подписчиков публикуются намеренно: издатель сообщает обо всех значимых фактах, не зная,
кому они понадобятся (webhook партнёру — US-16, аналитика).

### Пример сообщения

Топик `shipment.events`, ключ `7b6f6f0e-2f55-4d6e-9d5c-3c1f8f1e2a10`:

```json
{
  "eventId": "c1d0b7a2-58c4-4f0e-8a51-2f7b0f3f9a01",
  "eventType": "shipment.created",
  "eventVersion": 1,
  "occurredAt": "2026-03-01T10:30:00.123456+00:00",
  "source": "shipment-service",
  "correlationId": "5f0c1a2e-7c0b-4a57-9d3e-0d4f7f1f8a11",
  "key": "7b6f6f0e-2f55-4d6e-9d5c-3c1f8f1e2a10",
  "payload": {
    "shipmentId": "7b6f6f0e-2f55-4d6e-9d5c-3c1f8f1e2a10",
    "partnerId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "externalOrderId": "ORD-2026-000124",
    "lockerId": "11111111-1111-4111-8111-111111111111",
    "parcelSize": "S",
    "paymentType": "ON_PICKUP",
    "amount": 149900,
    "currency": "RUB"
  }
}
```

## Поведение при сбоях интеграций

| Что отказало | Что видит пользователь | Что происходит в системе | Восстановление |
|--------------|------------------------|--------------------------|----------------|
| locker-service | Партнёр: отправление дольше остаётся в `CREATED`. Терминал: операции недоступны | События копятся в `shipment.events` | После запуска сервис дочитывает события, резервы выполняются |
| payment-service | Получатель: не может создать платёж. Выдача предоплаченных заказов работает | Webhook'и PSP получают `5xx`, PSP повторяет | После запуска обрабатываются повторные webhook'и |
| notification-service | Получатель не получает SMS | События копятся в топике | После запуска SMS отправляются с задержкой |
| Kafka | API отвечает, но изменения не распространяются | События копятся в таблицах `outbox` | После восстановления relay публикует накопленное по порядку |
| PSP | `502 PSP_UNAVAILABLE` при создании платежа | Ничего не меняется | Получатель повторяет попытку |
| PostgreSQL сервиса | `5xx` на запросах этого сервиса | Потребитель не коммитит offset | После восстановления события обрабатываются повторно, дубли отсекаются |
