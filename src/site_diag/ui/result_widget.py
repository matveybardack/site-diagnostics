from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from site_diag.domain.models import DiagnosisResult
from site_diag.ui.theme import set_state

PLACEHOLDER_TEXT = "Результат появится здесь после нажатия «Диагностировать»."

# вид зависит от категории определённости: (заголовок, уверенность, цвет)
_STYLES = {
    "established": ("Причина установлена", "высокая"),
    "probable": ("Вероятная причина", "средняя"),
    "insufficient": ("Недостаточно данных", "низкая"),
}
_ROW_HEIGHT = 26


def format_percent(probability: float) -> str:
    """0.6512 -> '65,1 %' (десятичная запятая, как принято в русскоязычном интерфейсе)."""
    return f"{probability * 100:.1f}".replace(".", ",") + " %"


class ResultWidget(QWidget):
    """Результат диагностики: лидер, вероятности, категория, объяснение."""

    def __init__(
        self,
        cause_labels: Mapping[str, str],
        symptom_labels: Mapping[str, str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._cause_labels = dict(cause_labels)
        self._symptom_labels = dict(symptom_labels)

        root = QVBoxLayout(self)

        self._placeholder = QLabel(PLACEHOLDER_TEXT)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setObjectName("placeholderLabel")
        root.addWidget(self._placeholder)

        self._content = QFrame()
        self._content.setObjectName("resultContent")
        content = QVBoxLayout(self._content)
        content.setContentsMargins(0, 0, 0, 0)

        self._conflict = QLabel()
        self._conflict.setObjectName("conflictLabel")
        self._conflict.setWordWrap(True)
        content.addWidget(self._conflict)

        self._category = QLabel()
        self._category.setObjectName("categoryLabel")
        content.addWidget(self._category)

        self._headline = QLabel()
        self._headline.setObjectName("headlineLabel")
        self._headline.setWordWrap(True)
        font = self._headline.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 4)
        self._headline.setFont(font)
        content.addWidget(self._headline)

        self._details = QLabel()
        self._details.setObjectName("detailsLabel")
        self._details.setWordWrap(True)
        content.addWidget(self._details)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Причина", "Вероятность"])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(1, 220)
        content.addWidget(self._table)

        self._explanation_title = QLabel("Почему так:")
        bold = self._explanation_title.font()
        bold.setBold(True)
        self._explanation_title.setFont(bold)
        content.addWidget(self._explanation_title)
        self._explanation = QLabel()
        self._explanation.setObjectName("explanationLabel")
        self._explanation.setWordWrap(True)
        content.addWidget(self._explanation)

        root.addWidget(self._content)

        self._save_status = QLabel()
        self._save_status.setObjectName("saveStatusLabel")
        self._save_status.setWordWrap(True)
        root.addWidget(self._save_status)
        root.addStretch(1)

        self.clear()

    # публичный интерфейс

    def clear(self) -> None:
        """Возвращает вид «результата ещё нет»."""
        self._content.hide()
        self._placeholder.show()
        self.clear_save_status()

    def show_result(self, result: DiagnosisResult) -> None:
        title, confidence_word = _STYLES[result.certainty]
        leader = self._cause_labels.get(result.leader_code, result.leader_code)

        # предупреждение о противоречиях
        if result.conflicts:
            pairs = "; ".join(
                f"«{self._symptom_labels.get(a, a)}» и «{self._symptom_labels.get(b, b)}»"
                for a, b in result.conflicts
            )
            self._conflict.setText(
                f"Внимание: отмеченные симптомы противоречат друг другу: {pairs}. "
                "Проверьте ввод, результат может быть неточным."
            )
            self._conflict.show()
        else:
            self._conflict.hide()

        # заголовок и основная строка зависят от категории (цвета в styles.qss)
        self._category.setText(title)
        percent = format_percent(result.confidence)
        if result.certainty == "insufficient":
            self._headline.setText("Недостаточно данных для уверенного вывода")
            self._details.setText(
                f"Самая вероятная из причин по расчёту: {leader} ({percent}), "
                "но надёжность вывода низкая. Отметьте больше симптомов "
                "или уточните имеющиеся."
            )
        else:
            self._headline.setText(leader)
            self._details.setText(
                f"Вероятность: {percent}  ·  Уверенность модели: {confidence_word}"
                f"  ·  Важность поломки: {result.importance} из 5"
            )
        for label in (self._category, self._headline, self._details):
            set_state(label, "certainty", result.certainty)

        self._fill_table(result, muted=result.certainty == "insufficient")

        if result.explanation:
            self._explanation.setText("\n".join(f"• {line}" for line in result.explanation))
            self._explanation_title.show()
            self._explanation.show()
        else:
            self._explanation_title.hide()
            self._explanation.hide()

        self._placeholder.hide()
        self._content.show()

    def set_save_status(self, message: str, *, error: bool = False) -> None:
        """Строка о сохранении запроса: подтверждение или ошибка (результат остаётся на экране)."""
        self._save_status.setText(message)
        set_state(self._save_status, "state", "error" if error else "ok")

    def clear_save_status(self) -> None:
        self._save_status.clear()

    # внутреннее

    def _fill_table(self, result: DiagnosisResult, *, muted: bool) -> None:
        scores = result.scores
        self._table.setRowCount(len(scores))
        for row, score in enumerate(scores):
            name = QTableWidgetItem(self._cause_labels.get(score.code, score.code))
            if muted:
                name.setForeground(Qt.GlobalColor.gray)
            self._table.setItem(row, 0, name)

            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(round(score.probability * 1000))
            bar.setFormat(format_percent(score.probability))
            bar.setTextVisible(True)
            bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bar.setEnabled(not muted)
            self._table.setCellWidget(row, 1, bar)

        self._table.setFixedHeight(
            self._table.horizontalHeader().height() + len(scores) * _ROW_HEIGHT + 4
        )
