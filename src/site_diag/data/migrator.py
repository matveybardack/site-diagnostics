from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

from site_diag.exceptions import DatabaseUnavailableError, MigrationError

log = logging.getLogger(__name__)

_FILE_RE = re.compile(r"^(\d+)_[A-Za-z0-9_]+\.sql$")
_LOCK_KEY = 727001  # произвольное число: общий замок на время миграций


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def discover_migrations(directory: Path) -> list[Migration]:
    """Читает migrations/*.sql, упорядочивает по номеру в имени файла."""
    if not directory.is_dir():
        raise MigrationError(f"Каталог миграций не найден: {directory}")

    found: dict[int, Migration] = {}
    for path in directory.glob("*.sql"):
        match = _FILE_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"Имя файла миграции не соответствует шаблону NNN_name.sql: {path.name}"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"Два файла миграции с номером {version}: "
                f"{found[version].name}, {path.name}"
            )
        # переводы строк нормализуются, чтобы контрольная сумма
        # не зависела от Windows/Linux и настройки autocrlf
        text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        found[version] = Migration(version, path.name, text, checksum)

    return [found[v] for v in sorted(found)]


class Migrator:
    def __init__(self, dsn: str, directory: Path):
        self._dsn = dsn
        self._directory = directory

    def apply_pending(self) -> list[str]:
        """Применяет ещё не применённые миграции, возвращает их имена.

        Каждая миграция выполняется в своей транзакции: при ошибке она
        откатывается целиком, а уже применённые предыдущие остаются.
        Повторный запуск без новых файлов возвращает пустой список.
        """
        migrations = discover_migrations(self._directory)

        try:
            conn = psycopg.connect(self._dsn, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError as exc:
            raise DatabaseUnavailableError(
                f"Не удалось подключиться для миграций: {exc}"
            ) from exc

        applied_now: list[str] = []
        try:
            # общий замок: два приложения не мигрируют одновременно
            conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version    integer PRIMARY KEY,
                        name       text NOT NULL,
                        checksum   text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                done = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT version, checksum FROM schema_migrations"
                    )
                }
                known = {m.version for m in migrations}
                for version in sorted(set(done) - known):
                    log.warning(
                        "В БД применена миграция %s, файла для неё нет", version
                    )

                for m in migrations:
                    if m.version in done:
                        if done[m.version] != m.checksum:
                            raise MigrationError(
                                f"Файл {m.name} изменён после применения. "
                                "Схема меняется только новой миграцией."
                            )
                        continue
                    self._apply(conn, m)
                    applied_now.append(m.name)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
        finally:
            conn.close()

        if applied_now:
            log.info("Применены миграции: %s", ", ".join(applied_now))
        else:
            log.info("Новых миграций нет")
        return applied_now

    @staticmethod
    def _apply(conn: psycopg.Connection, m: Migration) -> None:
        try:
            with conn.transaction():
                conn.execute(m.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) "
                    "VALUES (%s, %s, %s)",
                    (m.version, m.name, m.checksum),
                )
        except psycopg.Error as exc:
            raise MigrationError(f"Миграция {m.name} не применена: {exc}") from exc
