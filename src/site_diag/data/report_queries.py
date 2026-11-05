from __future__ import annotations

from site_diag.data.connection import Database
from site_diag.domain.models import (
    HistogramPoint,
    ReportFilter,
    RequestRecord,
    SortSpec,
)
from site_diag.exceptions import ValidationError

# имена колонок подставляются в SQL только из этого словаря, не из ввода
_SORT_COLUMNS = {
    "submitted_at": "r.submitted_at",
    "top_cause_code": "r.top_cause_code",
    "confidence": "r.confidence",
    "importance": "r.importance",
}

DEFAULT_SORT = SortSpec("importance", True)  # важность ↓, затем время ↓


def _where(f: ReportFilter) -> tuple[str, list]:
    """WHERE по фильтру. Значения передаются параметрами, не в тексте SQL."""
    parts: list[str] = []
    params: list = []

    def add(condition: str, value) -> None:
        if value is not None:
            parts.append(condition)
            params.append(value)

    add("r.submitted_at >= %s", f.date_from)  # левая граница включается
    add("r.submitted_at < %s", f.date_to)  # правая не включается
    add("r.top_cause_code = %s", f.cause_code)
    add("r.confidence >= %s", f.min_confidence)
    add("r.confidence <= %s", f.max_confidence)
    add("r.importance >= %s", f.min_importance)
    add("r.importance <= %s", f.max_importance)

    return (" WHERE " + " AND ".join(parts)) if parts else "", params


def _order_by(sort: SortSpec | None) -> str:
    sort = sort or DEFAULT_SORT
    column = _SORT_COLUMNS.get(sort.column)
    if column is None:
        raise ValidationError(f"Недопустимая колонка сортировки: {sort.column!r}")
    direction = "DESC" if sort.descending else "ASC"
    if sort.column == "importance":
        secondary = "r.submitted_at DESC"  # вторичный ключ из требований
    elif sort.column == "submitted_at":
        secondary = ""
    else:
        secondary = "r.submitted_at DESC"
    parts = [f"{column} {direction}"]
    if secondary:
        parts.append(secondary)
    parts.append("r.id DESC")  # детерминированный порядок для страниц
    return " ORDER BY " + ", ".join(parts)


class ReportQueries:
    def __init__(self, db: Database):
        self._db = db

    def list_requests(
        self,
        f: ReportFilter,
        sort: SortSpec | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RequestRecord]:
        """Страница запросов. Вероятности по причинам не загружаются (scores пуст)."""
        if limit < 1 or offset < 0:
            raise ValidationError("limit должен быть >= 1, offset >= 0")
        where, params = _where(f)
        sql = (
            """
            SELECT r.id, r.submitted_at, r.free_text, r.top_cause_code,
                   r.confidence, r.importance, r.certainty_category,
                   r.model_version,
                   ARRAY(
                       SELECT rs.symptom_code
                       FROM request_symptoms rs
                       JOIN symptoms s ON s.code = rs.symptom_code
                       WHERE rs.request_id = r.id
                       ORDER BY s.sort_order
                   ) AS symptom_codes
            FROM diagnosis_requests r
            """
            + where
            + _order_by(sort)
            + " LIMIT %s OFFSET %s"
        )
        with self._db.connection() as conn:
            rows = conn.execute(sql, [*params, limit, offset]).fetchall()
        return [
            RequestRecord(
                id=r["id"],
                submitted_at=r["submitted_at"],
                free_text=r["free_text"],
                top_cause_code=r["top_cause_code"],
                confidence=r["confidence"],
                importance=r["importance"],
                certainty_category=r["certainty_category"],
                model_version=r["model_version"],
                symptom_codes=tuple(r["symptom_codes"]),
            )
            for r in rows
        ]

    def count_requests(self, f: ReportFilter) -> int:
        where, params = _where(f)
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM diagnosis_requests r" + where, params
            ).fetchone()
        return row["n"]

    def counts_by_hour(self, f: ReportFilter, tz: str = "UTC") -> list[HistogramPoint]:
        """Число запросов по часам суток 0-23 в часовом поясе tz.

        Возвращаются только часы, в которых есть данные; нулями заполняет
        report_service (F-4).
        """
        where, params = _where(f)
        sql = (
            "SELECT EXTRACT(hour FROM r.submitted_at AT TIME ZONE %s)::int AS bucket, "
            "count(*) AS n FROM diagnosis_requests r"
            + where
            + " GROUP BY 1 ORDER BY 1"
        )
        with self._db.connection() as conn:
            rows = conn.execute(sql, [tz, *params]).fetchall()
        return [HistogramPoint(r["bucket"], r["n"]) for r in rows]

    def counts_by_day(self, f: ReportFilter, tz: str = "UTC") -> list[HistogramPoint]:
        """Число запросов по календарным датам в часовом поясе tz.

        Только даты с данными; пропуски заполняет report_service (F-4).
        """
        where, params = _where(f)
        sql = (
            "SELECT (r.submitted_at AT TIME ZONE %s)::date AS bucket, "
            "count(*) AS n FROM diagnosis_requests r"
            + where
            + " GROUP BY 1 ORDER BY 1"
        )
        with self._db.connection() as conn:
            rows = conn.execute(sql, [tz, *params]).fetchall()
        return [HistogramPoint(r["bucket"], r["n"]) for r in rows]

    def cause_distribution(self, f: ReportFilter) -> list[tuple[str, int]]:
        """(код причины, число запросов): по убыванию числа, при равенстве по коду."""
        where, params = _where(f)
        sql = (
            "SELECT r.top_cause_code AS code, count(*) AS n "
            "FROM diagnosis_requests r"
            + where
            + " GROUP BY r.top_cause_code ORDER BY n DESC, code"
        )
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(r["code"], r["n"]) for r in rows]

    def top_important(self, f: ReportFilter, n: int) -> list[RequestRecord]:
        """Топ-N самых важных поломок: важность ↓, время ↓."""
        return self.list_requests(f, DEFAULT_SORT, limit=n, offset=0)

    def avg_confidence(self, f: ReportFilter) -> float | None:
        """Средняя уверенность или None, если запросов нет."""
        where, params = _where(f)
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT avg(r.confidence) AS a FROM diagnosis_requests r" + where,
                params,
            ).fetchone()
        return row["a"]
