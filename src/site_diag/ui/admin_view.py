from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QModelIndex, Qt, QTimer
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedLayout,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from site_diag.context import AppContext
from site_diag.domain.models import ReportFilter, User
from site_diag.services.export_service import export_csv
from site_diag.services.report_service import MAX_PAGE_SIZE
from site_diag.ui.diagnosis_view import user_text
from site_diag.ui.filters_panel import FiltersPanel
from site_diag.ui.histogram_widget import HistogramWidget
from site_diag.ui.login_dialog import LoginDialog
from site_diag.ui.report_widget import ReportWidget
from site_diag.ui.request_details_dialog import RequestDetailsDialog
from site_diag.ui.requests_table_model import (
    COL_CAUSE,
    COL_CONFIDENCE,
    COL_IMPORTANCE,
    COL_SYMPTOMS,
    COL_TIME,
    RequestsTableModel,
)
from site_diag.ui.theme import set_state
from site_diag.ui.workers import Worker, submit

log = logging.getLogger(__name__)

LOADING_TEXT = "Загрузка…"
EMPTY_TEXT = "Запросов по выбранным фильтрам нет. Измените фильтры или выполните диагностику."
NOT_LOADED_TEXT = "Данные не загружены."
LOCKED_TEXT = "Раздел доступен только администратору."


class AdminView(QWidget):
    """Раздел администратора: вход, общие фильтры, вкладки «Запросы / Отчёт / Гистограмма».

    До входа показывается страница с кнопкой «Войти». Данные запрашиваются при
    входе, при смене фильтра и по кнопке «Обновить»; переключение вкладок
    запросов к БД не делает.
    """

    def __init__(self, ctx: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self._ctx = ctx
        self._zone = ZoneInfo(ctx.settings.report_timezone)
        self._tz = ctx.settings.report_timezone
        self._symptom_labels = {s.code: s.label for s in ctx.symptoms}
        self._cause_labels = {c.code: c.label for c in ctx.causes}
        self._user: User | None = None
        self._auto_prompted = False
        self._exporting = False

        self._stack = QStackedLayout(self)
        self._stack.addWidget(self._build_locked_page())  # 0: вход
        self._stack.addWidget(self._build_content_page())  # 1: содержимое

    # построение

    def _build_locked_page(self) -> QWidget:
        page = QWidget()
        label = QLabel(LOCKED_TEXT)
        label.setObjectName("placeholderLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        button = QPushButton("Войти…")
        button.setObjectName("diagnoseButton")
        button.clicked.connect(self.prompt_login)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        row.addStretch(1)
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        layout.addWidget(label)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _build_content_page(self) -> QWidget:
        ctx = self._ctx
        page = QWidget()

        # верхняя строка
        self._user_label = QLabel()
        refresh = QPushButton("Обновить")
        refresh.setToolTip("Перезагрузить данные с текущими фильтрами")
        refresh.clicked.connect(self.refresh)
        logout = QPushButton("Выйти")
        logout.clicked.connect(self.logout)
        top = QHBoxLayout()
        top.addWidget(self._user_label)
        top.addStretch(1)
        top.addWidget(refresh)
        top.addWidget(logout)

        # фильтры
        self._filters = FiltersPanel(ctx.causes, self._tz)
        self._filters.filter_changed.connect(self._on_filter_changed)

        # вкладки
        self._model = RequestsTableModel(
            ctx.reports, self._symptom_labels, self._cause_labels, self._tz
        )
        self._report = ReportWidget(ctx.reports, self._cause_labels, self._tz)
        self._histogram = HistogramWidget(ctx.reports, self._cause_labels, self._tz)
        self._histogram.filter_description_provider = self._filters.description
        self._report.summary_changed.connect(self._histogram.set_summary)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_requests_tab(), "Запросы")
        self._tabs.addTab(self._report, "Отчёт")
        self._tabs.addTab(self._histogram, "Гистограмма")

        layout = QVBoxLayout(page)
        layout.addLayout(top)
        layout.addWidget(self._filters)
        layout.addWidget(self._tabs, 1)
        return page

    def _build_requests_tab(self) -> QWidget:
        tab = QWidget()

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.verticalHeader().hide()
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_SYMPTOMS, QHeaderView.ResizeMode.Stretch)
        for col in (COL_TIME, COL_CAUSE, COL_CONFIDENCE, COL_IMPORTANCE):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setSortingEnabled(True)
        # режим по умолчанию: важность ↓ (вторичный ключ «время ↓» задаёт слой данных)
        self._table.sortByColumn(COL_IMPORTANCE, Qt.SortOrder.DescendingOrder)
        self._table.doubleClicked.connect(self._open_details)
        self._table.setToolTip("Двойной щелчок открывает карточку запроса")

        self._table_message = QLabel(LOADING_TEXT)
        self._table_message.setObjectName("placeholderLabel")
        self._table_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table_message.setWordWrap(True)

        self._table_stack = QStackedLayout()
        self._table_stack.addWidget(self._table)  # 0: таблица
        self._table_stack.addWidget(self._table_message)  # 1: заглушка

        # ошибка загрузки страницы + повтор
        self._error_label = QLabel()
        self._error_label.setObjectName("reportError")
        self._error_label.setWordWrap(True)
        self._retry_button = QPushButton("Повторить")
        self._retry_button.clicked.connect(self._model.retry)
        self._error_bar = QWidget()
        error_layout = QHBoxLayout(self._error_bar)
        error_layout.setContentsMargins(0, 0, 0, 0)
        error_layout.addWidget(self._error_label, 1)
        error_layout.addWidget(self._retry_button)
        self._error_bar.hide()

        # экспорт
        self._export_status = QLabel()
        self._export_status.setObjectName("saveStatusLabel")
        self._export_button = QPushButton("Экспорт CSV…")
        self._export_button.setToolTip("Сохранить все записи по текущим фильтрам в CSV")
        self._export_button.setEnabled(False)
        self._export_button.clicked.connect(self._on_export_csv)
        bottom = QHBoxLayout()
        bottom.addWidget(self._export_status, 1)
        bottom.addWidget(self._export_button)

        layout = QVBoxLayout(tab)
        layout.addLayout(self._table_stack, 1)
        layout.addWidget(self._error_bar)
        layout.addLayout(bottom)

        self._model.total_changed.connect(self._on_total_changed)
        self._model.loading_changed.connect(self._on_loading_changed)
        self._model.load_failed.connect(self._on_load_failed)
        return tab

    # вход и выход

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        # один раз сам предлагаем войти при первом открытии вкладки;
        # после отмены остаётся кнопка «Войти…»
        if not self._auto_prompted and self._user is None:
            self._auto_prompted = True
            QTimer.singleShot(0, self._auto_prompt)

    def _auto_prompt(self) -> None:
        if self.isVisible():
            self.prompt_login()

    def prompt_login(self) -> None:
        if self._user is not None:
            return
        user = LoginDialog.ask(self._ctx.auth, self)
        if user is not None:
            self._unlock(user)

    def _unlock(self, user: User) -> None:
        self._user = user
        self._report.user_id = user.id  # для журнала аудита
        self._user_label.setText(f"Администратор: {user.login}")
        self._stack.setCurrentIndex(1)
        self._on_filter_changed(self._filters.current_filter())

    def logout(self) -> None:
        self._user = None
        self._report.user_id = None
        self._export_status.clear()
        self._stack.setCurrentIndex(0)

    @property
    def user(self) -> User | None:
        return self._user

    # данные

    def _on_filter_changed(self, report_filter: ReportFilter) -> None:
        self._filters.set_found_count(None)
        self._error_bar.hide()
        self._export_status.clear()
        self._export_button.setEnabled(False)
        self._model.set_filter(report_filter)
        self._report.set_filter(report_filter)
        self._histogram.set_filter(report_filter)

    def refresh(self) -> None:
        """Перезагружает все три вкладки с текущим фильтром."""
        self._filters.set_found_count(None)
        self._error_bar.hide()
        self._export_button.setEnabled(False)
        self._model.reload()
        self._report.reload()
        self._histogram.reload()

    # таблица

    def _show_table_message(self, text: str) -> None:
        self._table_message.setText(text)
        self._table_stack.setCurrentIndex(1)

    def _on_loading_changed(self, loading: bool) -> None:
        if loading:
            self._error_bar.hide()
            if self._model.rowCount() == 0:
                self._show_table_message(LOADING_TEXT)

    def _on_total_changed(self, total: int) -> None:
        self._filters.set_found_count(total)
        if total == 0:  # I-4: заглушка вместо пустой таблицы, экспорт отключён
            self._show_table_message(EMPTY_TEXT)
        else:
            self._table_stack.setCurrentIndex(0)
        self._export_button.setEnabled(total > 0 and not self._exporting)

    def _on_load_failed(self, text: str) -> None:
        self._error_label.setText(f"Не удалось загрузить запросы: {text}")
        self._error_bar.show()
        if self._model.rowCount() == 0:
            self._show_table_message(NOT_LOADED_TEXT)

    def _open_details(self, index: QModelIndex) -> None:
        record = self._model.record(index.row())
        if record is None:
            return
        dialog = RequestDetailsDialog(
            self._ctx.diagnosis,
            record,
            self._symptom_labels,
            self._cause_labels,
            self._tz,
            self,
        )
        dialog.exec()
        dialog.deleteLater()

    # экспорт CSV

    def _on_export_csv(self) -> None:
        if self._exporting or not self._model.total:
            return
        suggested = f"requests_{datetime.now(self._zone):%Y%m%d_%H%M}.csv"
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Экспорт запросов", suggested, "CSV (*.csv)"
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() != ".csv":
            path = path.with_name(path.name + ".csv")
            if path.exists():  # системный диалог проверял другое имя
                answer = QMessageBox.question(
                    self, "Экспорт запросов", f"Файл {path.name} уже существует. Заменить?"
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

        # фильтр и сортировка фиксируются в момент нажатия: экспорт повторяет таблицу
        report_filter = self._filters.current_filter()
        sort = self._model.sort_spec
        reports = self._ctx.reports
        symptom_labels, cause_labels, tz = self._symptom_labels, self._cause_labels, self._tz

        def task() -> tuple[int, str]:
            rows, seen, page = [], set(), 0
            while True:  # все страницы, а не только подгруженные в таблицу
                chunk = reports.get_page(report_filter, sort, page, MAX_PAGE_SIZE)
                rows.extend(r for r in chunk.items if r.id not in seen)
                seen.update(r.id for r in chunk.items)
                if not chunk.items or len(seen) >= chunk.total:
                    break
                page += 1
            count = export_csv(
                rows, path, symptom_labels=symptom_labels, cause_labels=cause_labels, tz=tz
            )
            return count, path.name

        self._exporting = True
        self._export_button.setEnabled(False)
        self._export_status.setText("Экспорт…")
        set_state(self._export_status, "state", "ok")
        worker = Worker(task)
        worker.signals.finished.connect(self._on_export_done)
        worker.signals.failed.connect(self._on_export_failed)
        submit(worker)

    def _on_export_done(self, result: tuple) -> None:
        count, name = result
        self._exporting = False
        self._export_button.setEnabled(bool(self._model.total))
        set_state(self._export_status, "state", "ok")
        self._export_status.setText(f"Сохранено: {name} ({count} записей)")

    def _on_export_failed(self, exc: BaseException) -> None:
        self._exporting = False
        self._export_button.setEnabled(bool(self._model.total))
        self._export_status.clear()
        QMessageBox.warning(self, "Экспорт запросов", user_text(exc))
