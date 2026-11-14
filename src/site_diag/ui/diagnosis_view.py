from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QSplitter, QWidget

from site_diag.context import AppContext
from site_diag.domain.models import DiagnosisResult
from site_diag.exceptions import AppError, ValidationError
from site_diag.ui.result_widget import ResultWidget
from site_diag.ui.symptom_form import SymptomForm
from site_diag.ui.workers import Worker, submit

log = logging.getLogger(__name__)

CALC_TEXT = "Идёт расчёт…"
SAVING_TEXT = "Сохранение…"
UNEXPECTED_TEXT = "Произошла непредвиденная ошибка. Подробности записаны в журнал."


def user_text(exc: BaseException) -> str:
    """Текст ошибки для пользователя: без трассировки и технических деталей."""
    return exc.user_message if isinstance(exc, AppError) else UNEXPECTED_TEXT


@dataclass(slots=True)
class _Run:
    """Данные одной диагностики, нужные для сохранения (в том числе повторного)."""

    codes: list[str]
    free_text: str
    submitted_at: datetime  # момент нажатия кнопки, а не записи в БД
    result: DiagnosisResult | None = None


class DiagnosisView(QWidget):
    """Вкладка диагностики: форма слева, результат справа.

    Порядок: нажатие → время → проверка ввода → расчёт (Worker) → результат
    на экране → сохранение (Worker). Если сохранение не удалось, результат
    остаётся на экране, а повторить сохранение можно через retry_save().
    """

    request_saved = Signal(int)  # id сохранённого запроса

    def __init__(self, ctx: AppContext, parent: QWidget | None = None):
        super().__init__(parent)
        self._ctx = ctx
        self._busy = False
        self._run: _Run | None = None  # выполняющаяся диагностика
        self._unsaved: _Run | None = None  # результат, который не удалось сохранить

        self._form = SymptomForm(ctx.symptoms)
        self._result = ResultWidget(
            {c.code: c.label for c in ctx.causes},
            {s.code: s.label for s in ctx.symptoms},
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._result)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._form)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)

        layout = QHBoxLayout(self)
        layout.addWidget(splitter)

        self._form.diagnose_requested.connect(self._on_diagnose_requested)
        self._form.cleared.connect(self._on_cleared)

    # диагностика

    def _on_diagnose_requested(self) -> None:
        if self._busy:
            return
        codes = self._form.selected_codes()
        text = self._form.free_text()
        submitted_at = datetime.now().astimezone()  # aware-время: поле БД timestamptz

        try:
            self._ctx.diagnosis.validate(codes, text)  # без БД, выполняется мгновенно
        except ValidationError as exc:
            self._form.show_warning(str(exc))
            return

        self._form.clear_warning()
        self._result.clear()
        self._unsaved = None
        self._run = _Run(codes, text, submitted_at)
        self._set_busy(True, CALC_TEXT)

        worker = Worker(self._ctx.diagnosis.diagnose, codes, text)
        worker.signals.finished.connect(self._on_diagnosed)
        worker.signals.failed.connect(self._on_diagnose_failed)
        submit(worker)

    def _on_diagnosed(self, result: DiagnosisResult) -> None:
        run = self._run
        if run is None:
            return
        run.result = result
        self._result.show_result(result)  # результат виден до сохранения
        self._start_save(run)

    def _on_diagnose_failed(self, exc: Exception) -> None:
        self._run = None
        self._set_busy(False)
        self._form.show_warning(f"{user_text(exc)} Попробуйте ещё раз.")

    # сохранение

    def _start_save(self, run: _Run) -> None:
        assert run.result is not None
        self._result.set_save_status(SAVING_TEXT)
        self._set_busy(True, SAVING_TEXT)

        worker = Worker(
            self._ctx.diagnosis.save,
            run.result,
            run.codes,
            run.submitted_at,
            run.free_text,
        )
        worker.signals.finished.connect(self._on_saved)
        worker.signals.failed.connect(self._on_save_failed)
        submit(worker)

    def _on_saved(self, request_id: int) -> None:
        self._run = None
        self._unsaved = None
        self._set_busy(False)
        self._result.set_save_status(f"Запрос сохранён (№ {request_id}).")
        self.request_saved.emit(request_id)

    def _on_save_failed(self, exc: Exception) -> None:
        # результат расчёта не теряется: он остаётся на экране
        self._unsaved = self._run
        self._run = None
        self._set_busy(False)
        self._result.set_save_status(
            f"Результат не сохранён: {user_text(exc)}", error=True
        )

    def has_unsaved_result(self) -> bool:
        return self._unsaved is not None

    def retry_save(self) -> None:
        """Повторяет сохранение результата, который остался на экране без записи."""
        if self._busy or self._unsaved is None:
            return
        self._run, self._unsaved = self._unsaved, None
        self._start_save(self._run)

    # прочее

    def _on_cleared(self) -> None:
        self._result.clear()
        self._unsaved = None  # форма очищена: сохранять уже нечего

    def _set_busy(self, busy: bool, text: str | None = None) -> None:
        self._busy = busy
        self._form.set_busy(busy, text)
