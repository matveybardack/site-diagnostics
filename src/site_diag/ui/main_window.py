from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QShortcut, QKeySequence
from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget, QWidget

from site_diag.context import AppContext
from site_diag.ui.theme import set_state

log = logging.getLogger(__name__)

DB_UNKNOWN = "БД: проверка…"
DB_OK = "БД: подключена"
DB_DOWN = "БД: нет связи, сохранение недоступно"
DEFAULT_CHECK_INTERVAL_MS = 5000


class _ProbeSignals(QObject):
    finished = Signal(bool)


class _ProbeTask(QRunnable):
    """Одна проверка доступности БД в рабочем потоке."""

    def __init__(self, check, signals: _ProbeSignals):
        super().__init__()
        self._check = check
        self._signals = signals

    def run(self) -> None:
        try:
            ok = bool(self._check())
        except Exception:
            log.exception("Сбой проверки доступности БД")
            ok = False
        try:
            self._signals.finished.emit(ok)
        except RuntimeError:
            pass  # окно уже уничтожено


def _placeholder(text: str) -> QWidget:
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


class MainWindow(QMainWindow):
    """Главное окно: вкладки диагностики и администратора, состояние БД и модели."""

    # True/False при смене состояния связи с БД
    db_status_changed = Signal(bool)

    def __init__(
        self,
        ctx: AppContext,
        *,
        db_check_interval_ms: int = DEFAULT_CHECK_INTERVAL_MS,
    ):
        super().__init__()
        self._ctx = ctx
        self._db_available: bool | None = None
        self._probe_running = False

        self.setWindowTitle("Диагностика отказа сайта")
        self.resize(960, 680)
        self.setMinimumSize(720, 520)

        self._tabs = QTabWidget()
        self._tabs.addTab(_placeholder("Форма диагностики появится в G-6"), "Диагностика")
        self._tabs.addTab(_placeholder("Раздел администратора появится в H-7"), "Администратор")
        self.setCentralWidget(self._tabs)

        self._db_label = QLabel(DB_UNKNOWN)
        self._db_label.setObjectName("dbStatus")
        self._model_label = QLabel(f"Модель: {ctx.model_version}")
        status = self.statusBar()
        status.addWidget(self._db_label, 1)
        status.addPermanentWidget(self._model_label)

        # отдельный однопоточный пул: проверка не конкурирует с пользовательскими операциями
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _ProbeSignals(self)
        self._signals.finished.connect(self._on_probe_finished)

        self._timer = QTimer(self)
        self._timer.setInterval(db_check_interval_ms)
        self._timer.timeout.connect(self.check_db_now)
        self._timer.start()
        QTimer.singleShot(0, self.check_db_now)  # первая проверка сразу после показа
        QShortcut(QKeySequence("Ctrl+1"), self).activated.connect(lambda: self._tabs.setCurrentIndex(0))
        QShortcut(QKeySequence("Ctrl+2"), self).activated.connect(lambda: self._tabs.setCurrentIndex(1))

    # ---------- вкладки ----------

    def set_diagnosis_view(self, widget: QWidget) -> None:
        self._replace_tab(0, widget, "Диагностика")

    def set_admin_view(self, widget: QWidget) -> None:
        self._replace_tab(1, widget, "Администратор")

    def _replace_tab(self, index: int, widget: QWidget, title: str) -> None:
        current = self._tabs.currentIndex()
        old = self._tabs.widget(index)
        self._tabs.removeTab(index)
        self._tabs.insertTab(index, widget, title)
        self._tabs.setCurrentIndex(current)
        if old is not None:
            old.deleteLater()

    # ---------- состояние БД ----------

    @property
    def db_available(self) -> bool | None:
        """None до первой проверки."""
        return self._db_available

    def check_db_now(self) -> None:
        """Запускает проверку в фоне; если предыдущая ещё идёт, ничего не делает."""
        if self._probe_running:
            return
        self._probe_running = True
        self._pool.start(_ProbeTask(self._ctx.is_db_available, self._signals))

    def _on_probe_finished(self, ok: bool) -> None:
        self._probe_running = False
        self._db_label.setText(DB_OK if ok else DB_DOWN)
        set_state(self._db_label, "state", "ok" if ok else "down")
        if ok != self._db_available:
            log.info("Состояние БД: %s", "подключена" if ok else "нет связи")
            self._db_available = ok
            self.db_status_changed.emit(ok)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._timer.stop()
        self._pool.waitForDone(3000)  # дождаться проверки до закрытия пула соединений
        super().closeEvent(event)
