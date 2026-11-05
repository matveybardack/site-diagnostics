from __future__ import annotations

import psycopg.errors

from site_diag.data.connection import Database
from site_diag.domain.models import User
from site_diag.exceptions import DuplicateLoginError, ValidationError

MAX_LOGIN_LENGTH = 64  # совпадает с CHECK в таблице users


class UserRepository:
    def __init__(self, db: Database):
        self._db = db

    def get_by_login(self, login: str) -> User | None:
        """Пользователь с ролью или None, если такого логина нет."""
        with self._db.connection() as conn:
            row = conn.execute(
                """
                SELECT u.id, u.login, u.password_hash, r.code AS role
                FROM users u
                JOIN roles r ON r.id = u.role_id
                WHERE u.login = %s
                """,
                (login.strip(),),
            ).fetchone()
        if row is None:
            return None
        return User(
            id=row["id"],
            login=row["login"],
            role=row["role"],
            password_hash=row["password_hash"],
        )

    def create_user(self, login: str, password_hash: str, role_code: str) -> User:
        """Создаёт пользователя и возвращает его.

        DuplicateLoginError: логин занят.
        ValidationError: пустой или слишком длинный логин, неизвестная роль.
        """
        login = login.strip()
        if not 1 <= len(login) <= MAX_LOGIN_LENGTH:
            raise ValidationError(
                f"Логин должен содержать от 1 до {MAX_LOGIN_LENGTH} символов"
            )
        if not password_hash:
            raise ValidationError("Хэш пароля не задан")

        try:
            with self._db.connection() as conn:
                row = conn.execute(
                    """
                    INSERT INTO users (login, password_hash, role_id)
                    SELECT %s, %s, r.id FROM roles r WHERE r.code = %s
                    RETURNING id
                    """,
                    (login, password_hash, role_code),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateLoginError(f"Логин уже занят: {login}") from exc

        if row is None:  # подзапрос по roles ничего не вернул
            raise ValidationError(f"Неизвестная роль: {role_code}")

        return User(
            id=row["id"], login=login, role=role_code, password_hash=password_hash
        )
