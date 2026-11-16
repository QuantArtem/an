"""Доменная модель отправления: статусы и разрешённые переходы.

Чистый модуль без инфраструктуры — покрыт unit-тестами (tests/unit).
Диаграмма состояний: docs/03-architecture/state-machines.md
"""
from enum import StrEnum


class Status(StrEnum):
    CREATED = "CREATED"                    # принято от магазина, ждём резерв ячейки
    CELL_RESERVED = "CELL_RESERVED"        # ячейка забронирована, ждём курьера
    READY_FOR_PICKUP = "READY_FOR_PICKUP"  # посылка в ячейке, код отправлен получателю
    DELIVERED = "DELIVERED"                # получатель забрал посылку
    REJECTED = "REJECTED"                  # не удалось забронировать ячейку
    CANCELLED = "CANCELLED"                # отменено магазином или истёк резерв
    EXPIRED = "EXPIRED"                    # истёк срок хранения, ждём изъятия курьером
    RETURNED = "RETURNED"                  # курьер изъял посылку для возврата магазину


class PaymentStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"  # предоплаченный заказ
    AWAITING = "AWAITING"          # требуется оплата при получении
    PAID = "PAID"


TRANSITIONS: dict[Status, set[Status]] = {
    Status.CREATED: {Status.CELL_RESERVED, Status.REJECTED, Status.CANCELLED},
    Status.CELL_RESERVED: {Status.READY_FOR_PICKUP, Status.CANCELLED},
    Status.READY_FOR_PICKUP: {Status.DELIVERED, Status.EXPIRED},
    Status.EXPIRED: {Status.RETURNED},
    Status.DELIVERED: set(),
    Status.REJECTED: set(),
    Status.CANCELLED: set(),
    Status.RETURNED: set(),
}

FINAL_STATUSES = {s for s, nxt in TRANSITIONS.items() if not nxt}


def can_transition(src: Status | str, dst: Status | str) -> bool:
    return Status(dst) in TRANSITIONS[Status(src)]


def initial_payment_status(payment_type: str) -> PaymentStatus:
    return PaymentStatus.AWAITING if payment_type == "ON_PICKUP" else PaymentStatus.NOT_REQUIRED
