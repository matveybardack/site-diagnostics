from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

from site_diag.data.connection import Database

log = logging.getLogger(__name__)


class AuditEvent:
    """Типы событий журнала (задача 27 из todo.md)."""

    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    REPORT_GENERATED = "report_generated"
    DIAGNOSIS_SAVED = "diagnosis_saved"
    INFERENCE_ERROR = "inference_error"


class AuditRepository:
    def __init__(self, db: Database):
        self._db = db

    def log(
        self,
        event_type: str,
        user_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Пишет событие в audit_log. Возвращает True, если запись сохранена.

        Исключения не выбрасываются: сбой журналирования не должен ломать
        операцию, ради которой событие записывалось.
        """
        try:
            with self._db.connection() as conn:
                conn.execute(
                    "INSERT INTO audit_log (event_type, user_id, details) "
                    "VALUES (%s, %s, %s)",
                    (event_type, user_id, Jsonb(details) if details is not None else None),
                )
            return True
        except Exception:  # noqa: BLE001 - намеренно: журнал не имеет права ронять приложение
            log.warning(
                "Не удалось записать событие аудита %r", event_type, exc_info=True
            )
            return False
