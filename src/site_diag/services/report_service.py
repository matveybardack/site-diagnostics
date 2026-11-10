from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from site_diag.data.audit_repository import AuditEvent, AuditRepository
from site_diag.data.report_queries import ReportQueries
from site_diag.domain.models import (
    HistogramMode,
    HistogramPoint,
    ReportFilter,
    ReportSummary,
    RequestPage,
    SortSpec,
)
from site_diag.exceptions import ConfigError, ValidationError

log = logging.getLogger(__name__)

MAX_PAGE_SIZE = 1000
MAX_TOP_N = 100
MAX_HISTOGRAM_DAYS = 3660  # около 10 лет: защита от гигантского списка нулей


class ReportService:
    def __init__(
        self,
        queries: ReportQueries,
        audit_repo: AuditRepository | None = None,
        timezone: str = "UTC",
    ):
        try:
            self._tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"Неизвестный часовой пояс: {timezone!r}") from None
        self._tz_name = timezone
        self._queries = queries
        self._audit = audit_repo

    @staticmethod
    def _check_filter(f: ReportFilter) -> None:
        for name in ("date_from", "date_to"):
            value = getattr(f, name)
            if value is not None and value.tzinfo is None:
                raise ValidationError(
                    f"{name} должен содержать часовой пояс (используйте aware-datetime)"
                )

    # таблица

    def get_page(
        self,
        f: ReportFilter,
        sort: SortSpec | None = None,
        page: int = 0,
        page_size: int = 100,
    ) -> RequestPage:
        """Страница запросов (номер с 0). Сортировка по умолчанию: важность ↓, время ↓."""
        self._check_filter(f)
        if page < 0:
            raise ValidationError("Номер страницы не может быть отрицательным")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValidationError(f"Размер страницы: от 1 до {MAX_PAGE_SIZE}")
        items = self._queries.list_requests(f, sort, limit=page_size, offset=page * page_size)
        total = self._queries.count_requests(f)
        return RequestPage(tuple(items), total, page, page_size)

    # сводка

    def summary(
        self, f: ReportFilter, top_n: int = 10, user_id: int | None = None
    ) -> ReportSummary:
        """Сводный отчёт; формирование записывается в журнал аудита."""
        self._check_filter(f)
        if not 1 <= top_n <= MAX_TOP_N:
            raise ValidationError(f"top_n: от 1 до {MAX_TOP_N}")

        total = self._queries.count_requests(f)
        result = ReportSummary(
            total=total,
            avg_confidence=self._queries.avg_confidence(f),
            cause_counts=tuple(self._queries.cause_distribution(f)),
            top_important=tuple(self._queries.top_important(f, top_n)),
        )
        if self._audit is not None:
            applied = {
                k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in asdict(f).items()
                if v is not None
            }
            self._audit.log(
                AuditEvent.REPORT_GENERATED, user_id, {"total": total, "filter": applied}
            )
        return result

    # гистограмма

    def histogram(self, f: ReportFilter, mode: HistogramMode) -> list[HistogramPoint]:
        """Гистограмма с нулевыми столбцами для периодов без данных.

        mode='hour': всегда 24 точки (часы 0-23 в часовом поясе отчёта).
        mode='day': непрерывный ряд дат от начала до конца периода. Границы берутся
        из фильтра, а если их нет, из первой и последней даты с данными.
        Пустой список означает, что период определить нельзя (нет данных и границ).
        """
        self._check_filter(f)
        if mode == "hour":
            counts = {p.bucket: p.count for p in self._queries.counts_by_hour(f, self._tz_name)}
            return [HistogramPoint(h, counts.get(h, 0)) for h in range(24)]
        if mode != "day":
            raise ValidationError(f"Неизвестный режим гистограммы: {mode!r}")

        counts: dict[date, int] = {
            p.bucket: p.count for p in self._queries.counts_by_day(f, self._tz_name)
        }
        start = (
            f.date_from.astimezone(self._tz).date()
            if f.date_from
            else (min(counts) if counts else None)
        )
        end = (
            # правая граница фильтра не включается
            (f.date_to - timedelta(microseconds=1)).astimezone(self._tz).date()
            if f.date_to
            else (max(counts) if counts else None)
        )
        if start is None or end is None or end < start:
            return []
        days = (end - start).days + 1
        if days > MAX_HISTOGRAM_DAYS:
            raise ValidationError(
                f"Период слишком большой для гистограммы по дням ({days} дн.). "
                f"Сузьте период до {MAX_HISTOGRAM_DAYS} дней."
            )
        return [
            HistogramPoint(d, counts.get(d, 0))
            for d in (start + timedelta(days=i) for i in range(days))
        ]
