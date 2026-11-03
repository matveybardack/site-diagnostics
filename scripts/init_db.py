"""Инициализация БД: применяет миграции из каталога migrations/.

Запуск из корня проекта:  python scripts/init_db.py
Повторный запуск безопасен: уже применённые миграции пропускаются.
"""
from __future__ import annotations

import logging
import sys

from site_diag.config import load_settings
from site_diag.data.migrator import Migrator
from site_diag.exceptions import AppError
from site_diag.logging_setup import setup_logging

log = logging.getLogger("init_db")


def main() -> int:
    try:
        settings = load_settings()
        setup_logging(settings)
        applied = Migrator(settings.dsn, settings.migrations_dir).apply_pending()
    except AppError as exc:
        # полная причина в тексте исключения, пользовательский текст - в заголовке
        log.error("%s %s", exc.user_message, exc)
        print(f"Ошибка: {exc.user_message}\n{exc}", file=sys.stderr)
        return 1

    if applied:
        print(f"Применено миграций: {len(applied)}")
        for name in applied:
            print(f"  + {name}")
    else:
        print("Применено миграций: 0 (база уже актуальна)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
