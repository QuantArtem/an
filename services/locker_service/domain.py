"""Доменные правила постамата — чистые функции, покрыты unit-тестами."""
import hashlib
import hmac
import secrets

SIZE_ORDER = ["S", "M", "L"]
MAX_FAILED_ATTEMPTS = 5      # BR-08
BLOCK_MINUTES = 10           # BR-08
PICKUP_CODE_LENGTH = 6       # BR-07


def suitable_sizes(parcel_size: str) -> list[str]:
    """BR-03: посылку можно положить в ячейку своего размера или больше (от меньшей к большей)."""
    return SIZE_ORDER[SIZE_ORDER.index(parcel_size):]


def generate_pickup_code() -> str:
    """Криптостойкий 6-значный код (ведущие нули допустимы)."""
    return f"{secrets.randbelow(10 ** PICKUP_CODE_LENGTH):0{PICKUP_CODE_LENGTH}d}"


def hash_pickup_code(pepper: str, locker_id: str, code: str) -> str:
    """В БД хранится только HMAC кода: утечка БД не раскрывает действующие коды."""
    return hmac.new(pepper.encode(), f"{locker_id}:{code}".encode(), hashlib.sha256).hexdigest()
