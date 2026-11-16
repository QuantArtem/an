#!/usr/bin/env bash
# Проверка NFR-08: остановка потребителя и брокера не приводит к потере событий.
# Требует запущенный стенд: docker compose up -d --build
set -euo pipefail
GW=${GATEWAY_URL:-http://localhost:8080}
LOCKER=11111111-1111-4111-8111-111111111111

create() {
  curl -fsS -X POST "$GW/api/v1/shipments" -H 'X-API-Key: demo-partner-key' \
    -H "Idempotency-Key: $(python3 -c 'import uuid;print(uuid.uuid4())')" -H 'Content-Type: application/json' \
    -d "{\"externalOrderId\":\"RES-$RANDOM-$RANDOM\",\"lockerId\":\"$LOCKER\",\"parcelSize\":\"S\",
         \"recipient\":{\"name\":\"Тест\",\"phone\":\"+79160000000\"},\"payment\":{\"type\":\"PREPAID\",\"amount\":0}}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'
}
status() { curl -fsS "$GW/api/v1/shipments/$1" -H 'X-API-Key: demo-partner-key' | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])'; }
wait_status() {
  for _ in $(seq 1 90); do [ "$(status "$1")" = "$2" ] && return 0; sleep 1; done
  echo "FAIL: $1 не перешло в $2, статус $(status "$1")"; exit 1
}
cancel() { curl -fsS -o /dev/null -X POST "$GW/api/v1/shipments/$1/cancel" -H 'X-API-Key: demo-partner-key'; }

echo "== 1. Потребитель (locker-service) остановлен =="
docker compose stop locker-service >/dev/null 2>&1
A=$(create); sleep 3
echo "   отправление $A создано, статус: $(status "$A") (ожидается CREATED)"
[ "$(status "$A")" = CREATED ]
docker compose start locker-service >/dev/null 2>&1
wait_status "$A" CELL_RESERVED
echo "   OK: после запуска потребителя статус CELL_RESERVED"

echo "== 2. Брокер (Kafka) остановлен =="
docker compose stop kafka >/dev/null 2>&1
B=$(create); sleep 3
echo "   отправление $B создано при недоступной Kafka, статус: $(status "$B") (ожидается CREATED)"
[ "$(status "$B")" = CREATED ]
docker compose start kafka >/dev/null 2>&1
wait_status "$B" CELL_RESERVED
echo "   OK: событие дождалось в outbox и доставлено после восстановления брокера"

cancel "$A"; cancel "$B"
echo "== Все проверки пройдены =="
