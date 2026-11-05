from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from site_diag.exceptions import DatabaseUnavailableError

log = logging.getLogger(__name__)

# SQLSTATE, означающие «сервер недоступен или остановлен»
_SHUTDOWN_STATES = {"57P01", "57P02", "57P03"}


def _is_connection_error(exc: psycopg.OperationalError) -> bool:
    """Отличает потерю связи с БД от обычных серверных ошибок."""
    state = exc.sqlstate
    return state is None or state.startswith("08") or state in _SHUTDOWN_STATES


class Database:
    """Пул соединений с PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
        timeout: float = 5.0,
    ):
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": max(1, int(timeout)),
            },
            # соединение проверяется при выдаче: «мёртвое» заменяется новым
            check=ConnectionPool.check_connection,
            name="site_diag",
            open=False,
        )
        self._pool.open(wait=False)

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[psycopg.Connection]:
        """Соединение из пула."""
        try:
            with self._pool.connection(timeout=timeout) as conn:
                yield conn
        except PoolTimeout as exc:
            log.error("Нет соединения с БД (таймаут пула): %s", exc)
            raise DatabaseUnavailableError(f"Таймаут подключения к БД: {exc}") from exc
        except psycopg.OperationalError as exc:
            if not _is_connection_error(exc):
                raise
            log.error("Потеряно соединение с БД: %s", exc)
            raise DatabaseUnavailableError(f"Соединение с БД потеряно: {exc}") from exc

    def is_available(self) -> bool:
        """Короткая проверка доступности. Ждёт не дольше 2 секунд."""
        try:
            with self.connection(timeout=2.0) as conn:
                conn.execute("SELECT 1")
            return True
        except DatabaseUnavailableError:
            return False

    def close(self) -> None:
        self._pool.close()
