from __future__ import annotations

from site_diag.data.connection import Database
from site_diag.domain.models import Cause, Symptom


class ReferenceRepository:
    def __init__(self, db: Database):
        self._db = db

    def list_symptoms(self) -> list[Symptom]:
        """Все симптомы в порядке отображения в форме (sort_order)."""
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT code, label, group_name, sort_order "
                "FROM symptoms ORDER BY sort_order"
            ).fetchall()
        return [
            Symptom(
                code=r["code"],
                label=r["label"],
                group=r["group_name"],
                sort_order=r["sort_order"],
            )
            for r in rows
        ]

    def list_causes(self) -> list[Cause]:
        """Все причины, упорядоченные по коду (порядок стабилен между запусками)."""
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT code, label, base_severity FROM causes ORDER BY code"
            ).fetchall()
        return [
            Cause(
                code=r["code"],
                label=r["label"],
                base_severity=r["base_severity"],
            )
            for r in rows
        ]
