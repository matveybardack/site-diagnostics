"""Создаёт пользователя с ролью admin.

Запуск из корня проекта:
    python scripts/create_admin.py
    python scripts/create_admin.py --login admin
Пароль запрашивается без отображения на экране и нигде не сохраняется открытым текстом.
"""
from __future__ import annotations

import argparse
import logging
import sys
from getpass import getpass

from site_diag.config import load_settings
from site_diag.data.connection import Database
from site_diag.data.user_repository import UserRepository
from site_diag.domain.models import User
from site_diag.exceptions import AppError
from site_diag.logging_setup import setup_logging
from site_diag.services.auth_service import hash_password

MIN_PASSWORD_LENGTH = 8

log = logging.getLogger("create_admin")


def create_admin(repo: UserRepository, login: str, password: str) -> User:
    return repo.create_user(login, hash_password(password), "admin")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Создание администратора")
    parser.add_argument("--login", help="логин (если не указан, будет запрошен)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        setup_logging(settings)

        login = (args.login or input("Логин администратора: ")).strip()
        password = getpass("Пароль: ")
        if password != getpass("Повторите пароль: "):
            print("Ошибка: пароли не совпадают.", file=sys.stderr)
            return 1
        if len(password) < MIN_PASSWORD_LENGTH:
            print(
                f"Ошибка: пароль должен содержать не менее {MIN_PASSWORD_LENGTH} символов.",
                file=sys.stderr,
            )
            return 1

        db = Database(settings.dsn)
        try:
            user = create_admin(UserRepository(db), login, password)
        finally:
            db.close()
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено.", file=sys.stderr)
        return 130
    except AppError as exc:
        log.error("%s %s", exc.user_message, exc)
        print(f"Ошибка: {exc.user_message}\n{exc}", file=sys.stderr)
        return 1

    log.info("Создан администратор id=%s login=%s", user.id, user.login)
    print(f"Администратор «{user.login}» создан.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
