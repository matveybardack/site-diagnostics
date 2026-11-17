from __future__ import annotations

import logging
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSpinBox,
    QStackedLayout,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from site_diag.domain.models import ReportFilter, ReportSummary
from site_diag.exceptions import AppError
from site_diag.services.report_service import MAX_TOP_N, ReportService
from site_diag.ui.diagnosis_view import user_text
from site_diag.ui.result_widget import format_percent
from site_diag.ui.workers import Worker, submit

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 10
EMPTY_TEXT = "За выбранный период запросов нет. Измените фильтры или выполните диагностику."
LOADING_TEXT = "Загрузка…"
_RELOAD_DELAY_MS = 400  # пауза после последнего щелчка по счётчику «топ-N»


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return table


def _item(text: str, *, center: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if center:
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    return item


class ReportWidget(QWidget):
    """Сводный отчёт: итоги, распределение по причинам, топ важных поломок.

    Загружается при смене фильтра (set_filter) или по reload(). Каждое
    формирование отчёта записывается в журнал аудита (ReportService.summary).
    Устаревшие ответы (фильтр сменился во время загрузки) отбрасываются.
    """

    summary_changed = Signal(object)  # ReportSummary или None, пока данных нет

    def __init__(
        self,
        reports: ReportService,
        cause_labels: Mapping[str, str],
        timezone: str = "UTC",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._reports = reports
        self._cause_labels = dict(cause_labels)
        self._zone = ZoneInfo(timezone)
        self._filter: ReportFilter | None = None
        self._generation = 0
        self._loading = False
        self._summary: ReportSummary | None = None
        self.user_id: int | None = None  # администратор, формирующий отчёт (для аудита)

        # итоги
        self._total_value = QLabel("—")
        self._total_value.setObjectName("statValue")
        self._avg_value = QLabel("—")
        self._avg_value.setObjectName("statValue")
        stats = QHBoxLayout()
        stats.addLayout(self._stat_block("Всего запросов", self._total_value))
        stats.addLayout(self._stat_block("Средняя уверенность", self._avg_value))
        stats.addStretch(1)

        # распределение по причинам
        self._cause_table = _table(["Причина", "Запросов", "Доля"])
        self._cause_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._cause_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        cause_box = QGroupBox("Распределение по причинам")
        QVBoxLayout(cause_box).addWidget(self._cause_table)

        # топ важных
        self._top_spin = QSpinBox()
        self._top_spin.setRange(1, MAX_TOP_N)
        self._top_spin.setValue(DEFAULT_TOP_N)
        self._top_spin.setToolTip("Сколько самых важных поломок показывать")
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(_RELOAD_DELAY_MS)
        self._reload_timer.timeout.connect(self.reload)
        self._top_spin.valueChanged.connect(lambda _: self._reload_timer.start())

        self._top_table = _table(["Причина", "Время", "Уверенность", "Важность"])
        for col in (1, 2, 3):
            self._top_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        top_box = QGroupBox("Наиболее важные поломки")
        top_layout = QVBoxLayout(top_box)
        selector = QHBoxLayout()
        selector.addWidget(QLabel("Показать:"))
        selector.addWidget(self._top_spin)
        selector.addStretch(1)
        top_layout.addLayout(selector)
        top_layout.addWidget(self._top_table)

        tables = QHBoxLayout()
        tables.addWidget(cause_box, 1)
        tables.addWidget(top_box, 1)

        data = QWidget()
        data_layout = QVBoxLayout(data)
        data_layout.setContentsMargins(0, 0, 0, 0)
        data_layout.addLayout(stats)
        data_layout.addLayout(tables, 1)

        # заглушки: пустая выборка, загрузка, ошибка
        self._message = QLabel()
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)

        self._stack = QStackedLayout(self)
        self._stack.addWidget(data)  # 0: данные
        self._stack.addWidget(self._message)  # 1: заглушка
        self._show_message(LOADING_TEXT, "placeholderLabel")

    @staticmethod
    def _stat_block(title: str, value: QLabel) -> QVBoxLayout:
        block = QVBoxLayout()
        caption = QLabel(title)
        caption.setObjectName("placeholderLabel")  # приглушённый серый из styles.qss
        block.addWidget(caption)
        block.addWidget(value)
        return block

    # управление

    @property
    def current_summary(self) -> ReportSummary | None:
        """Последняя загруженная сводка (для экспорта) или None."""
        return self._summary

    @property
    def top_n(self) -> int:
        return self._top_spin.value()

    def set_filter(self, report_filter: ReportFilter) -> None:
        self._filter = report_filter
        self.reload()

    def reload(self) -> None:
        if self._filter is None:
            return
        self._reload_timer.stop()
        self._generation += 1
        generation, f, top_n, user_id = self._generation, self._filter, self.top_n, self.user_id
        reports = self._reports

        def task():
            # исключение возвращается данными, чтобы знать поколение запроса
            try:
                return generation, reports.summary(f, top_n, user_id), None
            except Exception as exc:
                if isinstance(exc, AppError):
                    log.warning("Не удалось сформировать сводку: %s", exc, exc_info=True)
                else:
                    log.exception("Неожиданная ошибка при формировании сводки")
                return generation, None, exc

        self._loading = True
        if self._summary is None:  # поверх уже показанных данных надпись не выводим
            self._show_message(LOADING_TEXT, "placeholderLabel")
        worker = Worker(task)
        worker.signals.finished.connect(self._on_loaded)
        submit(worker)

    # результат

    def _on_loaded(self, payload: tuple) -> None:
        generation, summary, exc = payload
        if generation != self._generation:
            return  # устарел: фильтр сменился во время загрузки
        self._loading = False

        if exc is not None:
            self._set_summary(None)
            self._show_message(f"Не удалось сформировать отчёт: {user_text(exc)}", "reportError")
            return

        self._set_summary(summary)
        if summary.total == 0:
            self._show_message(EMPTY_TEXT, "placeholderLabel")  # вместо пустых таблиц (I-4)
            return
        self._fill(summary)
        self._stack.setCurrentIndex(0)

    def _set_summary(self, summary: ReportSummary | None) -> None:
        self._summary = summary
        self.summary_changed.emit(summary)

    def _show_message(self, text: str, object_name: str) -> None:
        self._message.setObjectName(object_name)
        self._message.setText(text)
        self._message.style().unpolish(self._message)  # новое имя объекта: перечитать QSS
        self._message.style().polish(self._message)
        self._stack.setCurrentIndex(1)

    def _fill(self, summary: ReportSummary) -> None:
        self._total_value.setText(str(summary.total))
        self._avg_value.setText(
            format_percent(summary.avg_confidence) if summary.avg_confidence is not None else "—"
        )

        self._cause_table.setRowCount(len(summary.cause_counts))
        for row, (code, count) in enumerate(summary.cause_counts):
            self._cause_table.setItem(row, 0, _item(self._cause_labels.get(code, code)))
            self._cause_table.setItem(row, 1, _item(str(count), center=True))
            self._cause_table.setItem(row, 2, _item(format_percent(count / summary.total), center=True))

        self._top_table.setRowCount(len(summary.top_important))
        for row, rec in enumerate(summary.top_important):
            when = rec.submitted_at.astimezone(self._zone).strftime("%d.%m.%Y %H:%M")
            self._top_table.setItem(row, 0, _item(self._cause_labels.get(rec.top_cause_code, rec.top_cause_code)))
            self._top_table.setItem(row, 1, _item(when))
            self._top_table.setItem(row, 2, _item(format_percent(rec.confidence), center=True))
            self._top_table.setItem(row, 3, _item(str(rec.importance), center=True))
