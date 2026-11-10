from __future__ import annotations

import logging
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from site_diag.data.audit_repository import AuditEvent, AuditRepository
from site_diag.data.user_repository import UserRepository
from site_diag.domain.models import User
from site_diag.exceptions import AccessDeniedError

log = logging.getLogger(__name__)

# параметры по умолчанию argon2-cffi (Argon2id), соль генерируется автоматически
_hasher = PasswordHasher()

_MAX_LOGGED_LOGIN = 64


def hash_password(password: str) -> str:
    """Хэш Argon2id со случайной солью. Соль и параметры хранятся внутри строки."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True, если пароль подходит к хэшу. Повреждённый хэш даёт False, не исключение."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


class AuthService:
    """Вход администратора.

    Неверный логин, неверный пароль и недостаточная роль неотличимы для
    пользователя: во всех случаях AccessDeniedError с одним и тем же текстом.
    Причина записывается только в журнал аудита и в лог.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        audit_repo: AuditRepository,
        required_role: str = "admin",
    ):
        self._users = user_repo
        self._audit = audit_repo
        self._required_role = required_role
        # для выравнивания времени ответа, когда логина нет в БД
        self._dummy_hash = hash_password(secrets.token_urlsafe(16))

    def login(self, login: str, password: str) -> User:
        """Возвращает пользователя с требуемой ролью.

        AccessDeniedError: пустые данные, неизвестный логин, неверный пароль
        или роль не подходит.
        DatabaseUnavailableError: БД недоступна.
        """
        if (
            not isinstance(login, str)
            or not isinstance(password, str)
            or not login.strip()
            or not password
        ):
            raise AccessDeniedError()
        login = login.strip()

        user = self._users.get_by_login(login)
        reason: str | None = None
        if user is None:
            # хэширование всё равно выполняется, чтобы по времени ответа
            # нельзя было отличить несуществующий логин от неверного пароля
            verify_password(self._dummy_hash, password)
            reason = "unknown_user"
        elif not verify_password(user.password_hash, password):
            reason = "bad_password"
        elif user.role != self._required_role:
            reason = "role_denied"

        if reason is not None:
            self._audit.log(
                AuditEvent.LOGIN_FAILURE,
                user.id if user else None,
                {"login": login[:_MAX_LOGGED_LOGIN], "reason": reason},
            )
            log.warning("Неудачный вход: login=%r причина=%s", login[:_MAX_LOGGED_LOGIN], reason)
            raise AccessDeniedError()

        self._audit.log(AuditEvent.LOGIN_SUCCESS, user.id, {"login": user.login})
        log.info("Вход выполнен: login=%s role=%s", user.login, user.role)
        return user
