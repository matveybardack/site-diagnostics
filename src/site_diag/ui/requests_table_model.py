from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)

from site_diag.domain.models import ReportFilter, RequestRecord, SortSpec
from site_diag.exceptions import AppError
from site_diag.services.report_service import ReportService
from site_diag.ui.diagnosis_view import user_text
from site_diag.ui.result_widget import format_percent
from site_diag.ui.workers import Worker, submit

log = logging.getLogger(__name__)

COLUMNS = ("Время", "Симптомы", "Причина", "Уверенность", "Важность")
COL_TIME, COL_SYMPTOMS, COL_CAUSE, COL_CONFIDENCE, COL_IMPORTANCE = range(5)

# колонка -> поле сортировки в БД; «Симптомы» в БД не сортируются (агрегат из другой таблицы)
_SORT_FIELDS = {
    COL_TIME: "submitted_at",
    COL_CAUSE: "top_cause_code",
    COL_CONFIDENCE: "confidence",
    COL_IMPORTANCE: "importance",
}
_CERTAINTY_RU = {
    "established": "причина установлена",
    "probable": "вероятная причина",
    "insufficient": "недостаточно данных",
}
_ANY_INDEX = QModelIndex | QPersistentModelIndex
DEFAULT_PAGE_SIZE = 100


class RequestsTableModel(QAbstractTableModel):
    """Таблица запросов с постраничной подгрузкой.

    Без явной сортировки порядок «важность ↓, время ↓» (задаёт слой данных).
    Все изменения фильтра и сортировки сбрасывают таблицу и грузят первую
    страницу заново. Результаты устаревших загрузок отбрасываются.
    """

    total_changed = Signal(int)  # всего записей по фильтру (для счётчика «найдено»)
    loading_changed = Signal(bool)
    load_failed = Signal(str)  # текст для пользователя; повтор: retry()

    def __init__(
        self,
        reports: ReportService,
        symptom_labels: dict[str, str],
        cause_labels: dict[str, str],
        timezone: str = "UTC",
        page_size: int = DEFAULT_PAGE_SIZE,
        parent: Any = None,
    ):
        super().__init__(parent)
        self._reports = reports
        self._symptom_labels = dict(symptom_labels)
        self._cause_labels = dict(cause_labels)
        self._zone = ZoneInfo(timezone)
        self._page_size = page_size

        self._rows: list[RequestRecord] = []
        self._ids: set[int] = set()  # защита от дублей, если данные сдвинулись между страницами
        self._filter: ReportFilter | None = None
        self._sort: SortSpec | None = None  # None = порядок по умолчанию
        self._total: int | None = None
        self._next_page = 0
        self._generation = 0  # номер «поколения»: ответ другого поколения устарел
        self._loading = False
        self._failed = False
        self._exhausted = False

    # управление

    def set_filter(self, report_filter: ReportFilter) -> None:
        """Применяет фильтр и загружает первую страницу."""
        self._filter = report_filter
        self._reset()

    def reload(self) -> None:
        """Перезагружает таблицу с теми же фильтром и сортировкой."""
        self._reset()

    def retry(self) -> None:
        """Повторяет страницу, загрузка которой не удалась."""
        if self._failed:
            self._failed = False
            self._load_next()

    @property
    def total(self) -> int | None:
        """Число записей по фильтру или None, пока первая страница не получена."""
        return self._total

    @property
    def is_loading(self) -> bool:
        return self._loading

    def record(self, row: int) -> RequestRecord | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    # загрузка

    def _reset(self) -> None:
        self._generation += 1
        self.beginResetModel()
        self._rows = []
        self._ids = set()
        self._total = None
        self._next_page = 0
        self._failed = False
        self._exhausted = False
        self.endResetModel()
        self._set_loading(False)  # идущая загрузка устарела: её ответ будет отброшен
        if self._filter is not None:
            self._load_next()

    def _set_loading(self, loading: bool) -> None:
        if loading != self._loading:
            self._loading = loading
            self.loading_changed.emit(loading)

    def _load_next(self) -> None:
        if self._loading or self._filter is None:
            return
        generation, report_filter = self._generation, self._filter
        sort, page, size, reports = self._sort, self._next_page, self._page_size, self._reports

        def task():
            # исключение возвращается как данные, чтобы знать поколение запроса
            try:
                return generation, reports.get_page(report_filter, sort, page, size), None
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, AppError):
                    log.warning("Не удалось загрузить страницу запросов: %s", exc, exc_info=True)
                else:
                    log.exception("Неожиданная ошибка при загрузке страницы запросов")
                return generation, None, exc

        self._set_loading(True)
        worker = Worker(task)
        worker.signals.finished.connect(self._on_loaded)
        submit(worker)

    def _on_loaded(self, payload: tuple) -> None:
        generation, page, exc = payload
        if generation != self._generation:
            return  # фильтр или сортировка изменились: ответ устарел
        self._set_loading(False)

        if exc is not None:
            self._failed = True  # без этого view запрашивал бы страницу снова и снова
            self.load_failed.emit(user_text(exc))
            return

        new = [r for r in page.items if r.id not in self._ids]
        if new:
            first = len(self._rows)
            self.beginInsertRows(QModelIndex(), first, first + len(new) - 1)
            self._rows.extend(new)
            self._ids.update(r.id for r in new)
            self.endInsertRows()

        self._next_page += 1
        if not page.items:
            self._exhausted = True
        if page.total != self._total:
            self._total = page.total
            self.total_changed.emit(page.total)

    def canFetchMore(self, parent: _ANY_INDEX = QModelIndex()) -> bool:  # noqa: N802
        if (
            parent.isValid()
            or self._filter is None
            or self._loading
            or self._failed
            or self._exhausted
        ):
            return False
        return self._total is None or len(self._rows) < self._total

    def fetchMore(self, parent: _ANY_INDEX = QModelIndex()) -> None:  # noqa: N802
        if not parent.isValid():
            self._load_next()

    # сортировка

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        field = _SORT_FIELDS.get(column)
        if field is None:
            return  # колонка не сортируется базой
        new = SortSpec(field, order == Qt.SortOrder.DescendingOrder)
        if new == self._sort:
            return
        self._sort = new
        self._reset()

    # отображение

    def rowCount(self, parent: _ANY_INDEX = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _ANY_INDEX = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(  # noqa: N802
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return section + 1

    def _symptoms_text(self, record: RequestRecord) -> str:
        return ", ".join(self._symptom_labels.get(c, c) for c in record.symptom_codes)

    def data(self, index: _ANY_INDEX, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        record, column = self._rows[index.row()], index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if column == COL_TIME:
                return record.submitted_at.astimezone(self._zone).strftime("%d.%m.%Y %H:%M:%S")
            if column == COL_SYMPTOMS:
                return self._symptoms_text(record)
            if column == COL_CAUSE:
                return self._cause_labels.get(record.top_cause_code, record.top_cause_code)
            if column == COL_CONFIDENCE:
                return format_percent(record.confidence)
            if column == COL_IMPORTANCE:
                return record.importance
        elif role == Qt.ItemDataRole.ToolTipRole:
            if column == COL_SYMPTOMS:
                return self._symptoms_text(record)
            if column == COL_CAUSE:
                return (
                    f"{self._cause_labels.get(record.top_cause_code, record.top_cause_code)}\n"
                    f"{_CERTAINTY_RU.get(record.certainty_category, record.certainty_category)}"
                )
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if column in (COL_CONFIDENCE, COL_IMPORTANCE):
                return int(Qt.AlignmentFlag.AlignCenter)
        elif role == Qt.ItemDataRole.UserRole:
            return record
        return None
