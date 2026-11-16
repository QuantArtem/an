"""Контракты пишутся вручную (design-first), поэтому проверяем, что код от них не уехал (NFR-34).

* REST: множество «метод + путь» в OpenAPI совпадает с фактическими маршрутами приложения.
* События: каждое публикуемое и каждое обрабатываемое событие описано в AsyncAPI, и наоборот.
* Ошибки: каждый код ошибки из кода описан в каталоге ошибок.
"""
import importlib
import os
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "05-integration"
SERVICES = ["shipment", "locker", "payment", "notification"]
os.environ.setdefault("DATABASE_URL", "postgresql://unused")  # приложение импортируется без подключения к БД


def source(service: str) -> str:
    return (ROOT / "services" / f"{service}_service" / "main.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("service", SERVICES)
def test_rest_api_matches_openapi(service):
    spec = yaml.safe_load((DOCS / "openapi" / f"{service}-service.yaml").read_text(encoding="utf-8"))
    documented = {(m.upper(), path) for path, item in spec["paths"].items() for m in item
                  if m in {"get", "post", "put", "patch", "delete"}}
    app = importlib.import_module(f"{service}_service.main").app
    actual = {(m.upper(), path.replace("_id}", "Id}")) for path, item in app.openapi()["paths"].items() for m in item}
    actual = {(m, re.sub(r"\{(\w+?)_?[iI]d\}", lambda g: "{" + g.group(1) + "Id}", p)) for m, p in actual}
    assert actual == documented


def asyncapi():
    return yaml.safe_load((DOCS / "asyncapi" / "lockerflow-events.yaml").read_text(encoding="utf-8"))


def test_events_match_asyncapi():
    spec = asyncapi()
    documented = {m["name"] for m in spec["components"]["messages"].values()} - {"dead-letter"}
    in_code = set()
    for service in SERVICES:
        in_code |= set(re.findall(r'"((?:shipment|locker|payment)\.[a-z_.]+)"', source(service)))
    in_code -= {"shipment.events", "locker.events", "payment.events"}
    assert in_code == documented


@pytest.mark.parametrize("service,operations", [
    ("shipment", ["shipmentServiceOnLockerEvents", "shipmentServiceOnPaymentEvents"]),
    ("locker", ["lockerServiceOnShipmentEvents"]),
    ("payment", ["paymentServiceOnShipmentEvents"]),
    ("notification", ["notificationServiceOnShipmentEvents"]),
])
def test_subscriptions_match_asyncapi(service, operations):
    spec = asyncapi()
    documented = set()
    for op in operations:
        for ref in spec["operations"][op]["messages"]:
            channel, key = re.match(r"#/channels/(\w+)/messages/(\w+)", ref["$ref"]).groups()
            message_ref = spec["channels"][channel]["messages"][key]["$ref"].rsplit("/", 1)[1]
            documented.add(spec["components"]["messages"][message_ref]["name"])
    handlers = importlib.import_module(f"{service}_service.main").HANDLERS
    assert set(handlers) == documented


def test_asyncapi_refs_resolve():
    spec = asyncapi()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref":
                    target = spec
                    for part in v.lstrip("#/").split("/"):
                        target = target[part]
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(spec)


def test_error_codes_are_documented():
    catalog = (DOCS / "error-catalog.md").read_text(encoding="utf-8")
    codes = set()
    for service in SERVICES:
        codes |= set(re.findall(r'ApiError\(\s*\d{3},\s*"([A-Z_]+)"', source(service)))
    codes.add("VALIDATION_ERROR")
    missing = {c for c in codes if f"`{c}`" not in catalog}
    assert not missing, f"коды ошибок не описаны в каталоге: {missing}"
