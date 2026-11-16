"""Таблица переходов статусов отправления (docs/03-architecture/state-machines.md)."""
import pytest
from shipment_service.domain import FINAL_STATUSES, PaymentStatus, Status, can_transition, initial_payment_status

ALLOWED = {
    ("CREATED", "CELL_RESERVED"), ("CREATED", "REJECTED"), ("CREATED", "CANCELLED"),
    ("CELL_RESERVED", "READY_FOR_PICKUP"), ("CELL_RESERVED", "CANCELLED"),
    ("READY_FOR_PICKUP", "DELIVERED"), ("READY_FOR_PICKUP", "EXPIRED"),
    ("EXPIRED", "RETURNED"),
}


@pytest.mark.parametrize("src", list(Status))
@pytest.mark.parametrize("dst", list(Status))
def test_transition_matrix(src, dst):
    assert can_transition(src, dst) == ((src.value, dst.value) in ALLOWED)


def test_final_statuses_have_no_exits():
    assert FINAL_STATUSES == {Status.DELIVERED, Status.REJECTED, Status.CANCELLED, Status.RETURNED}


def test_cannot_cancel_after_deposit():
    """BR-06: после закладки посылки отмена магазином невозможна."""
    assert not can_transition(Status.READY_FOR_PICKUP, Status.CANCELLED)


def test_initial_payment_status():
    assert initial_payment_status("ON_PICKUP") == PaymentStatus.AWAITING
    assert initial_payment_status("PREPAID") == PaymentStatus.NOT_REQUIRED
