from __future__ import annotations

from datetime import datetime

import psycopg.errors

from site_diag.data.connection import Database
from site_diag.domain.models import CauseScore, DiagnosisResult, RequestRecord
from site_diag.exceptions import ValidationError


class DiagnosisRepository:
    def __init__(self, db: Database):
        self._db = db

    def save(
        self,
        result: DiagnosisResult,
        symptom_codes: list[str] | tuple[str, ...],
        submitted_at: datetime,
        free_text: str | None = None,
    ) -> int:
        """Сохраняет запрос, его симптомы и вероятности всех причин. Возвращает id.

        Всё или ничего: при любой ошибке не остаётся ни одной из трёх записей.
        ValidationError: время без часового пояса или неизвестный код
        симптома/причины.
        """
        if submitted_at.tzinfo is None:
            raise ValidationError(
                "submitted_at должен содержать часовой пояс (timestamptz)"
            )
        text = (free_text or "").strip() or None
        codes = sorted(set(symptom_codes))  # дубли нарушили бы составной ключ

        try:
            with self._db.connection() as conn:
                request_id = conn.execute(
                    """
                    INSERT INTO diagnosis_requests
                        (submitted_at, free_text, top_cause_code, confidence,
                         importance, certainty_category, model_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        submitted_at,
                        text,
                        result.leader_code,
                        result.confidence,
                        result.importance,
                        result.certainty,
                        result.model_version,
                    ),
                ).fetchone()["id"]

                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO request_symptoms (request_id, symptom_code) "
                        "VALUES (%s, %s)",
                        [(request_id, code) for code in codes],
                    )
                    cur.executemany(
                        "INSERT INTO inference_results "
                        "(request_id, cause_code, probability) VALUES (%s, %s, %s)",
                        [(request_id, s.code, s.probability) for s in result.scores],
                    )
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ValidationError(
                f"Неизвестный код симптома или причины: {exc}"
            ) from exc

        return request_id

    def get_by_id(self, request_id: int) -> RequestRecord | None:
        """Запрос со всеми симптомами и вероятностями или None."""
        with self._db.connection() as conn:
            row = conn.execute(
                """
                SELECT id, submitted_at, free_text, top_cause_code, confidence,
                       importance, certainty_category, model_version
                FROM diagnosis_requests
                WHERE id = %s
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                return None

            symptoms = conn.execute(
                """
                SELECT rs.symptom_code
                FROM request_symptoms rs
                JOIN symptoms s ON s.code = rs.symptom_code
                WHERE rs.request_id = %s
                ORDER BY s.sort_order
                """,
                (request_id,),
            ).fetchall()
            scores = conn.execute(
                """
                SELECT cause_code, probability
                FROM inference_results
                WHERE request_id = %s
                ORDER BY probability DESC, cause_code
                """,
                (request_id,),
            ).fetchall()

        return RequestRecord(
            id=row["id"],
            submitted_at=row["submitted_at"],
            free_text=row["free_text"],
            top_cause_code=row["top_cause_code"],
            confidence=row["confidence"],
            importance=row["importance"],
            certainty_category=row["certainty_category"],
            model_version=row["model_version"],
            symptom_codes=tuple(r["symptom_code"] for r in symptoms),
            scores=tuple(
                CauseScore(r["cause_code"], r["probability"]) for r in scores
            ),
        )
