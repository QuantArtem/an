.PHONY: up down logs demo test e2e resilience lint

up:          ## Поднять стенд
	docker compose up -d --build

down:        ## Остановить стенд и удалить данные
	docker compose down -v

logs:        ## Цепочка обработанных событий
	docker compose logs -f | grep --line-buffered -E "event handled|SMS ->"

demo:        ## Сквозной сценарий с комментариями
	python3 scripts/demo.py

test:        ## Unit- и контрактные тесты (стенд не нужен)
	python3 -m pytest -q

e2e:         ## Сквозные сценарии на поднятом стенде
	python3 -m pytest tests/e2e -v

resilience:  ## Проверка сохранности событий при остановке сервиса и брокера
	./scripts/resilience_check.sh

lint:
	ruff check .
