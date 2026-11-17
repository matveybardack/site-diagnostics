from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

# matplotlib-бэкенд Qt импортируется после PySide6: так он выберет именно его
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from site_diag.domain.models import HistogramMode, HistogramPoint, ReportFilter, ReportSummary
from site_diag.exceptions import AppError
from site_diag.services.export_service import export_report
from site_diag.services.report_service import ReportService
from site_diag.ui.diagnosis_view import user_text
from site_diag.ui.theme import set_state
from site_diag.ui.workers import Worker, submit

log = logging.getLogger(__name__)

EMPTY_TEXT = "Нет данных для гистограммы. Измените фильтры или выполните диагностику."
LOADING_TEXT = "Загрузка…"
BAR_COLOR = "#4a90d9"
SHOW_VALUES_MAX_BARS = 31  # подписи над столбцами, пока их не слишком много


def draw_histogram(
    fig: Figure, mode: HistogramMode, points: Sequence[HistogramPoint], tz_name: str
) -> None:
    """Рисует гистограмму на figure. Не зависит от Qt. points не пуст.

    Нулевые столбцы остаются на оси: ряд непрерывный (его строит ReportService),
    просто их высота равна нулю.
    """
    fig.clear()
    ax = fig.add_subplot(111)
    xs = [p.bucket for p in points]
    counts = [p.count for p in points]
    total = sum(counts)
    bars = ax.bar(xs, counts, width=0.8, color=BAR_COLOR)

    if mode == "hour":
        ax.set_xticks(xs)
        ax.set_xticklabels([f"{h:02d}" for h in xs], fontsize=8)
        ax.set_xlim(-0.6, len(xs) - 0.4)
        ax.set_xlabel(f"Час суток ({tz_name})")
        ax.set_title(f"Запросы по часам суток (всего: {total})")
    else:
        locator = AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        ax.set_xlabel(f"Дата ({tz_name})")
        ax.set_title(f"Запросы по дням (всего: {total})")

    ax.set_ylabel("Число запросов")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_ylim(0, max(1, max(counts)) * 1.12)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    if len(points) <= SHOW_VALUES_MAX_BARS:
        ax.bar_label(bars, labels=[str(c) if c else "" for c in counts], padding=2, fontsize=8)


class HistogramWidget(QWidget):
    """Гистограмма обращений по часам суток или по дням + экспорт отчёта.

    Данные загружаются при смене фильтра (set_filter) или режима. Устаревшие
    ответы отбрасываются по номеру поколения. Для экспорта нужна сводка
    отчёта: её передают в set_summary.
    """

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
        self._tz_name = timezone
        self._zone = ZoneInfo(timezone)
        self._filter: ReportFilter | None = None
        self._summary: ReportSummary | None = None
        self._generation = 0
        self._loading = False
        self._has_data = False
        # возвращает текст активных фильтров для шапки PDF (FiltersPanel.description)
        self.filter_description_provider: Callable[[], str | None] | None = None

        # панель управления
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("По часам суток", "hour")
        self._mode_combo.addItem("По дням", "day")
        self._mode_combo.setToolTip("Как группировать запросы")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self._status = QLabel()
        self._status.setObjectName("saveStatusLabel")  # зелёный при state=ok

        self._export_button = QPushButton("Экспорт…")
        self._export_button.setToolTip("Сохранить сводку и график в PDF или график в PNG")
        self._export_button.setEnabled(False)
        self._export_button.clicked.connect(self._on_export)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Группировка:"))
        bar.addWidget(self._mode_combo)
        bar.addStretch(1)
        bar.addWidget(self._status)
        bar.addWidget(self._export_button)

        # график и заглушка
        self._figure = Figure(figsize=(8, 4.5), layout="constrained")
        self._canvas = FigureCanvasQTAgg(self._figure)
        self._message = QLabel()
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)

        self._stack = QStackedLayout()
        self._stack.addWidget(self._canvas)  # 0: график
        self._stack.addWidget(self._message)  # 1: заглушка

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addLayout(self._stack, 1)
        self._show_message(LOADING_TEXT, "placeholderLabel")

    # управление

    @property
    def mode(self) -> HistogramMode:
        return self._mode_combo.currentData()

    @property
    def figure(self) -> Figure:
        return self._figure

    def set_filter(self, report_filter: ReportFilter) -> None:
        self._filter = report_filter
        self.reload()

    def set_summary(self, summary: ReportSummary | None) -> None:
        """Слот для ReportWidget.summary_changed: нужна для экспорта."""
        self._summary = summary
        self._update_export_enabled()

    def reload(self) -> None:
        if self._filter is None:
            return
        self._generation += 1
        generation, f, mode = self._generation, self._filter, self.mode
        reports = self._reports

        def task():
            # исключение возвращается данными, чтобы знать поколение запроса
            try:
                return generation, mode, reports.histogram(f, mode), None
            except Exception as exc:
                if isinstance(exc, AppError):
                    log.warning("Не удалось построить гистограмму: %s", exc, exc_info=True)
                else:
                    log.exception("Неожиданная ошибка при построении гистограммы")
                return generation, mode, None, exc

        self._loading = True
        self._status.clear()
        self._update_export_enabled()
        if not self._has_data:  # поверх уже показанного графика надпись не выводим
            self._show_message(LOADING_TEXT, "placeholderLabel")
        worker = Worker(task)
        worker.signals.finished.connect(self._on_loaded)
        submit(worker)

    def _on_mode_changed(self, _index: int) -> None:
        self.reload()

    # результат

    def _on_loaded(self, payload: tuple) -> None:
        generation, mode, points, exc = payload
        if generation != self._generation:
            return  # устарел: фильтр или режим сменились во время загрузки
        self._loading = False

        if exc is not None:
            self._has_data = False
            self._update_export_enabled()
            self._show_message(f"Не удалось построить гистограмму: {user_text(exc)}", "reportError")
            return

        if not points or not any(p.count for p in points):
            # пустая выборка (I-4): заглушка вместо пустого графика, экспорт отключён
            self._has_data = False
            self._update_export_enabled()
            self._show_message(EMPTY_TEXT, "placeholderLabel")
            return

        draw_histogram(self._figure, mode, points, self._tz_name)
        self._canvas.draw_idle()
        self._has_data = True
        self._update_export_enabled()
        self._stack.setCurrentIndex(0)

    def _show_message(self, text: str, object_name: str) -> None:
        self._message.setObjectName(object_name)
        self._message.setText(text)
        self._message.style().unpolish(self._message)  # новое имя объекта: перечитать QSS
        self._message.style().polish(self._message)
        self._stack.setCurrentIndex(1)

    def _update_export_enabled(self) -> None:
        self._export_button.setEnabled(
            self._has_data and self._summary is not None and not self._loading
        )

    # экспорт

    def _on_export(self) -> None:
        if not self._has_data or self._summary is None:
            return
        suggested = f"histogram_{self.mode}_{datetime.now(self._zone):%Y%m%d_%H%M}.pdf"
        chosen_path, chosen_filter = QFileDialog.getSaveFileName(
            self, "Экспорт отчёта", suggested, "PDF (*.pdf);;PNG (*.png)"
        )
        if not chosen_path:
            return

        path = Path(chosen_path)
        if path.suffix.lower() not in (".pdf", ".png"):
            ext = ".png" if "png" in chosen_filter.lower() else ".pdf"
            path = path.with_name(path.name + ext)
            if path.exists():  # системный диалог проверял другое имя
                answer = QMessageBox.question(
                    self, "Экспорт отчёта", f"Файл {path.name} уже существует. Заменить?"
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

        description = (
            self.filter_description_provider() if self.filter_description_provider else None
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            export_report(
                self._summary,
                self._figure,
                path,
                cause_labels=self._cause_labels,
                filter_description=description,
                tz=self._tz_name,
            )
        except Exception as exc:
            if not isinstance(exc, AppError):
                log.exception("Неожиданная ошибка при экспорте отчёта")
            self._status.clear()
            QMessageBox.warning(self, "Экспорт отчёта", user_text(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        set_state(self._status, "state", "ok")
        self._status.setText(f"Сохранено: {path.name}")
