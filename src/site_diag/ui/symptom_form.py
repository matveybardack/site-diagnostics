from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEvent, Qt, Signal, QObject
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from site_diag.domain.models import Symptom
from site_diag.services.diagnosis_service import DEFAULT_MAX_FREE_TEXT

DIAGNOSE_TEXT = "Диагностировать"
BUSY_TEXT = "Идёт расчёт…"


class SymptomForm(QWidget):
    """Форма ввода симптомов: флажки по группам, свободный текст, две кнопки."""

    diagnose_requested = Signal()  # нажата «Диагностировать» (форма не заблокирована)
    cleared = Signal()  # нажата «Очистить»

    def __init__(
        self,
        symptoms: Sequence[Symptom],
        max_text_length: int = DEFAULT_MAX_FREE_TEXT,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._max_text = max_text_length
        self._boxes: dict[str, QCheckBox] = {}  # код симптома -> флажок, в порядке формы
        self._busy = False

        root = QVBoxLayout(self)

        title = QLabel("Что вы наблюдаете?")
        title.setObjectName("formTitle")
        font = title.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 2)
        title.setFont(font)
        root.addWidget(title)
        root.addWidget(QLabel("Отметьте все признаки сбоя, которые вы заметили."))

        # флажки лежат в прокручиваемой области: форма не должна «ломать» окно
        scroll = QScrollArea()
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        self._build_groups(content_layout, symptoms)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        root.addWidget(QLabel("Текст ошибки или свободное описание (необязательно):"))
        self._text = QPlainTextEdit()
        self._text.setPlaceholderText("Например, текст, который показал браузер")
        self._text.setFixedHeight(80)
        self._text.textChanged.connect(self._on_text_changed)
        root.addWidget(self._text)

        self._counter = QLabel(f"0 / {self._max_text}")
        self._counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        root.addWidget(self._counter)

        self._warning = QLabel()
        self._warning.setObjectName("warningLabel")
        self._warning.setWordWrap(True)
        self._warning.hide()
        root.addWidget(self._warning)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._clear_button = QPushButton("Очистить")
        self._clear_button.clicked.connect(self.clear)
        self._diagnose_button = QPushButton(DIAGNOSE_TEXT)
        self._diagnose_button.setObjectName("diagnoseButton")
        self._diagnose_button.clicked.connect(self._on_diagnose_clicked)
        buttons.addWidget(self._clear_button)
        buttons.addWidget(self._diagnose_button)
        root.addLayout(buttons)
        # подсказки
        self._text.setToolTip("Текст ошибки с экрана или любое уточнение. Необязательно.")
        self._counter.setToolTip("Длина описания / допустимый максимум")
        self._clear_button.setToolTip("Снять все отметки и очистить описание (Ctrl+L)")
        self._diagnose_button.setToolTip("Определить наиболее вероятную причину (Ctrl+Enter)")

        # горячие клавиши (WindowShortcut: работают, пока вкладка с формой видима)
        for sequence in ("Ctrl+Return", "Ctrl+Enter"):  # основная и цифровая клавиатура
            QShortcut(QKeySequence(sequence), self).activated.connect(self._on_diagnose_clicked)
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self.clear)
        # в текстовом поле Ctrl+Enter может быть перехвачен самим полем,
        # поэтому дополнительно ловим его фильтром событий
        self._text.installEventFilter(self)

        # порядок Tab: флажки по порядку формы, текст, «Очистить», «Диагностировать»
        chain = [*self._boxes.values(), self._text, self._clear_button, self._diagnose_button]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    # построение

    def _build_groups(self, layout: QVBoxLayout, symptoms: Sequence[Symptom]) -> None:
        if not symptoms:
            layout.addWidget(QLabel("Справочник симптомов пуст."))
            return

        groups: dict[str, QGroupBox] = {}  # порядок групп = порядок первого появления
        for symptom in sorted(symptoms, key=lambda s: s.sort_order):
            box = groups.get(symptom.group)
            if box is None:
                box = QGroupBox(symptom.group)
                box.setLayout(QVBoxLayout())
                groups[symptom.group] = box
                layout.addWidget(box)

            check = QCheckBox(symptom.label)
            check.setObjectName(f"symptom_{symptom.code}")
            check.toggled.connect(self.clear_warning)
            box.layout().addWidget(check)
            self._boxes[symptom.code] = check

    # состояние формы

    def selected_codes(self) -> list[str]:
        """Отмеченные симптомы в порядке формы (не в порядке кликов)."""
        return [code for code, box in self._boxes.items() if box.isChecked()]

    def free_text(self) -> str:
        return self._text.toPlainText().strip()

    def clear(self) -> None:
        """Снимает все отметки, очищает текст и предупреждение."""
        if self._busy:
            return
        for box in self._boxes.values():
            box.setChecked(False)
        self._text.clear()
        self.clear_warning()
        self.cleared.emit()

    def set_busy(self, busy: bool, text: str | None = None) -> None:
        """Блокирует ввод на время расчёта и повторное нажатие кнопки."""
        self._busy = busy
        for box in self._boxes.values():
            box.setEnabled(not busy)
        self._text.setEnabled(not busy)
        self._clear_button.setEnabled(not busy)
        self._diagnose_button.setEnabled(not busy)
        self._diagnose_button.setText((text or BUSY_TEXT) if busy else DIAGNOSE_TEXT)

    def show_warning(self, message: str) -> None:
        self._warning.setText(message)
        self._warning.show()

    def clear_warning(self, *_: object) -> None:
        self._warning.hide()
        self._warning.clear()

    # обработчики

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (
            obj is self._text
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._on_diagnose_clicked()
            return True
        return super().eventFilter(obj, event)

    def _on_diagnose_clicked(self) -> None:
        if not self._busy:
            self.diagnose_requested.emit()

    def _on_text_changed(self) -> None:
        text = self._text.toPlainText()
        if len(text) > self._max_text:
            position = min(self._text.textCursor().position(), self._max_text)
            self._text.blockSignals(True)
            self._text.setPlainText(text[: self._max_text])
            cursor = self._text.textCursor()
            cursor.setPosition(position)
            self._text.setTextCursor(cursor)
            self._text.blockSignals(False)
            text = text[: self._max_text]
        self._counter.setText(f"{len(text)} / {self._max_text}")
