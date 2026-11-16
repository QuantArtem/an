from locker_service.domain import generate_pickup_code, hash_pickup_code, suitable_sizes


def test_cell_size_upgrade_rule():
    """BR-03: посылка кладётся в ячейку своего размера или больше."""
    assert suitable_sizes("S") == ["S", "M", "L"]
    assert suitable_sizes("M") == ["M", "L"]
    assert suitable_sizes("L") == ["L"]


def test_pickup_code_format():
    """BR-07: код — ровно 6 цифр."""
    for _ in range(200):
        code = generate_pickup_code()
        assert len(code) == 6 and code.isdigit()


def test_pickup_code_hash_is_scoped_to_locker_and_pepper():
    h = hash_pickup_code("pepper", "locker-1", "123456")
    assert h == hash_pickup_code("pepper", "locker-1", "123456")
    assert h != hash_pickup_code("pepper", "locker-2", "123456")
    assert h != hash_pickup_code("other", "locker-1", "123456")
    assert "123456" not in h
