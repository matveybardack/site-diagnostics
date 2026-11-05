from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# параметры по умолчанию argon2-cffi (Argon2id), соль генерируется автоматически
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Хэш Argon2id со случайной солью. Соль и параметры хранятся внутри строки."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True, если пароль подходит к хэшу. Повреждённый хэш даёт False, не исключение."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False
