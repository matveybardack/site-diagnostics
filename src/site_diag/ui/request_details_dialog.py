from __future__ import annotations

import logging
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from site_diag.domain.models import DiagnosisResult, RequestDetails, RequestRecord
from site_diag.services.diagnosis_service import DiagnosisService
from site_diag.ui.diagnosis_view import user_text
from site_diag.ui.result_widget import ResultWidget
from site_diag.ui.workers import Worker, submit

log = logging.getLogger(__name__)

LOADING_TEXT = "Загрузка…"


def _value_label() -> QLabel:
    label = QLabel("—")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class RequestDetailsDialog(QDialog):
    """Карточка сохранённого запроса: симптомы, все вероятности, версия модели, пояснение.

    Строка из таблицы (record) даёт номер и время сразу; полные данные загружаются
    в фоне через DiagnosisService.get_request_details().
    """

    def __init__(
        self,
        diagnosis: DiagnosisService,
        record: RequestRecord,
        symptom_labels: Mapping[str, str],
        cause_labels: Mapping[str, str],
        timezone: str = "UTC",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._symptom_labels = dict(symptom_labels)
        self._cause_labels = dict(cause_labels)
        self._zone = ZoneInfo(timezone)
        self._tz_name = timezone
        self.setWindowTitle(f"Запрос № {record.id}")
        self.resize(720, 700)

        # сведения о запросе
        self._id_value = _value_label()
        self._time_value = _value_label()
        self._model_value = _value_label()
        self._symptoms_value = _value_label()
        self._text_value = _value_label()
        meta = QFormLayout()
        meta.setLabelAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        meta.addRow("Запрос:", self._id_value)
        meta.addRow("Время:", self._time_value)
        meta.addRow("Версия модели:", self._model_value)
        meta.addRow("Симптомы:", self._symptoms_value)
        meta.addRow("Описание:", self._text_value)

        # состояние загрузки / ошибка
        self._status = QLabel(LOADING_TEXT)
        self._status.setObjectName("placeholderLabel")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)

        # результат (тот же виджет, что и на вкладке диагностики)
        self._result = ResultWidget(cause_labels, symptom_labels)
        self._result.hide()

        self._note = QLabel()
        self._note.setObjectName("placeholderLabel")
        self._note.setWordWrap(True)
        self._note.hide()

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.addLayout(meta)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        body_layout.addWidget(separator)
        body_layout.addWidget(self._status)
        body_layout.addWidget(self._result)
        body_layout.addWidget(self._note)
        body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

        self._fill_meta(record)
        self._load(diagnosis, record.id)

    # загрузка

    def _load(self, diagnosis: DiagnosisService, request_id: int) -> None:
        def task() -> RequestDetails:
            return diagnosis.get_request_details(request_id)

        worker = Worker(task)  # ошибки Worker уже пишет в лог
        worker.signals.finished.connect(self._on_loaded)
        worker.signals.failed.connect(self._on_failed)
        submit(worker)

    def _on_loaded(self, details: RequestDetails) -> None:
        record = details.record
        self._fill_meta(record)
        self._result.show_result(
            DiagnosisResult(
                scores=record.scores,
                leader_code=record.top_cause_code,
                confidence=record.confidence,
                certainty=record.certainty_category,
                importance=record.importance,
                explanation=details.explanation,
                model_version=record.model_version,
            )
        )
        self._status.hide()
        self._result.show()
        if details.explanation_note:
            self._note.setText(details.explanation_note)
            self._note.show()

    def _on_failed(self, exc: BaseException) -> None:
        self._status.setObjectName("reportError")
        self._status.setText(f"Не удалось загрузить запрос: {user_text(exc)}")
        self._status.style().unpolish(self._status)  # новое имя объекта: перечитать QSS
        self._status.style().polish(self._status)

    # отображение

    def _fill_meta(self, record: RequestRecord) -> None:
        self._id_value.setText(f"№ {record.id}")
        when = record.submitted_at.astimezone(self._zone).strftime("%d.%m.%Y %H:%M:%S")
        self._time_value.setText(f"{when} ({self._tz_name})")
        self._model_value.setText(record.model_version or "—")
        self._symptoms_value.setText(
            "\n".join(f"• {self._symptom_labels.get(c, c)}" for c in record.symptom_codes) or "—"
        )
        self._text_value.setText(record.free_text or "—")
