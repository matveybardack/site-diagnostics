from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QWidget,
)

from site_diag.domain.models import Cause, ReportFilter

ALL_CAUSES = "Все причины"
DATE_FORMAT = "dd.MM.yyyy"


class FiltersPanel(QGroupBox):
    """Фильтры отчётов: период, причина, диапазоны уверенности и важности.

    Фильтр применяется кнопкой «Применить». Сигнал испускается только если
    фильтр действительно изменился, поэтому лишних перезапросов к БД нет.
    Даты понимаются в часовом поясе отчётов (REPORT_TIMEZONE): выбранный день
    охватывает сутки с 00:00 до 24:00 по этому поясу, конец периода включается.
    """

    filter_changed = Signal(object)  # ReportFilter

    def __init__(
        self,
        causes: Sequence[Cause],
        timezone: str = "UTC",
        parent: QWidget | None = None,
    ):
        super().__init__("Фильтры", parent)
        self._zone = ZoneInfo(timezone)
        self._cause_labels = {c.code: c.label for c in causes}
        self._applied = ReportFilter()

        # период
        self._from_check = QCheckBox("с")
        self._from_date = self._make_date()
        self._to_check = QCheckBox("по")
        self._to_date = self._make_date()
        self._from_check.toggled.connect(self._from_date.setEnabled)
        self._to_check.toggled.connect(self._to_date.setEnabled)

        period = QHBoxLayout()
        for widget in (self._from_check, self._from_date, self._to_check, self._to_date):
            period.addWidget(widget)
        period.addStretch(1)

        # причина
        self._cause = QComboBox()
        self._cause.addItem(ALL_CAUSES, None)
        for cause in sorted(causes, key=lambda c: c.label):
            self._cause.addItem(cause.label, cause.code)

        # диапазоны
        self._conf_min = self._make_spin(0, 100, " %")
        self._conf_max = self._make_spin(0, 100, " %")
        self._imp_min = self._make_spin(1, 5)
        self._imp_max = self._make_spin(1, 5)

        confidence = QHBoxLayout()
        confidence.addWidget(self._conf_min)
        confidence.addWidget(QLabel("—"))
        confidence.addWidget(self._conf_max)
        confidence.addStretch(1)

        importance = QHBoxLayout()
        importance.addWidget(self._imp_min)
        importance.addWidget(QLabel("—"))
        importance.addWidget(self._imp_max)
        importance.addStretch(1)

        # сообщения и кнопки
        self._warning = QLabel()
        self._warning.setObjectName("warningLabel")
        self._warning.setWordWrap(True)
        self._warning.hide()

        self._found = QLabel()
        self._reset_button = QPushButton("Сбросить")
        self._reset_button.clicked.connect(self.reset)
        self._apply_button = QPushButton("Применить")
        self._apply_button.setObjectName("diagnoseButton")  # акцентная кнопка из styles.qss
        self._apply_button.clicked.connect(self.apply)

        buttons = QHBoxLayout()
        buttons.addWidget(self._found)
        buttons.addStretch(1)
        buttons.addWidget(self._reset_button)
        buttons.addWidget(self._apply_button)

        grid = QGridLayout(self)
        grid.addWidget(QLabel("Период:"), 0, 0)
        grid.addLayout(period, 0, 1, 1, 3)
        grid.addWidget(QLabel("Причина:"), 1, 0)
        grid.addWidget(self._cause, 1, 1, 1, 3)
        grid.addWidget(QLabel("Уверенность:"), 2, 0)
        grid.addLayout(confidence, 2, 1)
        grid.addWidget(QLabel("Важность:"), 2, 2)
        grid.addLayout(importance, 2, 3)
        grid.addWidget(self._warning, 3, 0, 1, 4)
        grid.addLayout(buttons, 4, 0, 1, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self._from_check.setToolTip("Включить нижнюю границу периода")
        self._to_check.setToolTip("Включить верхнюю границу периода (день включается)")
        self._conf_min.setToolTip("Минимальная уверенность модели в лидирующей причине")
        self._conf_max.setToolTip("Максимальная уверенность модели в лидирующей причине")
        self._imp_min.setToolTip("Минимальная важность поломки (1-5)")
        self._imp_max.setToolTip("Максимальная важность поломки (1-5)")

        chain = [
            self._from_check, self._from_date, self._to_check, self._to_date,
            self._cause, self._conf_min, self._conf_max, self._imp_min, self._imp_max,
            self._reset_button, self._apply_button,
        ]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

        self._set_defaults()

    # построение

    @staticmethod
    def _make_date() -> QDateEdit:
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setDisplayFormat(DATE_FORMAT)
        return edit

    @staticmethod
    def _make_spin(low: int, high: int, suffix: str = "") -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setSuffix(suffix)
        return spin

    def _set_defaults(self) -> None:
        today = QDate.currentDate()
        for check, edit in ((self._from_check, self._from_date), (self._to_check, self._to_date)):
            check.setChecked(False)
            edit.setDate(today)
            edit.setEnabled(False)
        self._cause.setCurrentIndex(0)
        self._conf_min.setValue(0)
        self._conf_max.setValue(100)
        self._imp_min.setValue(1)
        self._imp_max.setValue(5)

    # чтение значений

    def _build_filter(self) -> ReportFilter:
        """ReportFilter из текущих значений. ValueError: неверные границы."""
        d_from = self._from_date.date().toPython() if self._from_check.isChecked() else None
        d_to = self._to_date.date().toPython() if self._to_check.isChecked() else None
        if d_from and d_to and d_from > d_to:
            raise ValueError("Начало периода позже его конца.")

        conf_min, conf_max = self._conf_min.value(), self._conf_max.value()
        if conf_min > conf_max:
            raise ValueError("Минимальная уверенность больше максимальной.")
        imp_min, imp_max = self._imp_min.value(), self._imp_max.value()
        if imp_min > imp_max:
            raise ValueError("Минимальная важность больше максимальной.")

        return ReportFilter(
            date_from=datetime.combine(d_from, time(), tzinfo=self._zone) if d_from else None,
            # правая граница в фильтре не включается: берём начало следующего дня
            date_to=(
                datetime.combine(d_to + timedelta(days=1), time(), tzinfo=self._zone)
                if d_to
                else None
            ),
            cause_code=self._cause.currentData(),
            # значения на границах диапазона означают «без ограничения»
            min_confidence=conf_min / 100 if conf_min > 0 else None,
            max_confidence=conf_max / 100 if conf_max < 100 else None,
            min_importance=imp_min if imp_min > 1 else None,
            max_importance=imp_max if imp_max < 5 else None,
        )

    # действия
    def current_filter(self) -> ReportFilter:
        """Последний применённый фильтр (до первого «Применить» пустой)."""
        return self._applied

    def apply(self) -> None:
        try:
            new = self._build_filter()
        except ValueError as exc:
            self._warning.setText(str(exc))
            self._warning.show()
            return
        self._warning.hide()
        if new != self._applied:
            self._applied = new
            self.filter_changed.emit(new)

    def reset(self) -> None:
        """Сбрасывает все поля; перезапрос данных только если фильтр был непустым."""
        self._set_defaults()
        self.apply()

    def set_found_count(self, total: int | None) -> None:
        """Счётчик «Найдено: N»; None очищает его (идёт загрузка)."""
        self._found.setText("" if total is None else f"Найдено записей: {total}")

    def description(self) -> str | None:
        """Текст применённых фильтров для шапки экспорта или None, если их нет."""
        f, parts = self._applied, []
        if f.date_from or f.date_to:
            start = f.date_from.astimezone(self._zone).strftime("%d.%m.%Y") if f.date_from else None
            end = (
                (f.date_to - timedelta(microseconds=1)).astimezone(self._zone).strftime("%d.%m.%Y")
                if f.date_to
                else None
            )
            parts.append(
                "период "
                + (f"с {start} " if start else "")
                + (f"по {end}" if end else "")
            )
        if f.cause_code:
            parts.append(f"причина: {self._cause_labels.get(f.cause_code, f.cause_code)}")
        if f.min_confidence is not None or f.max_confidence is not None:
            low = round((f.min_confidence or 0) * 100)
            high = round((f.max_confidence if f.max_confidence is not None else 1) * 100)
            parts.append(f"уверенность {low}–{high} %")
        if f.min_importance is not None or f.max_importance is not None:
            parts.append(f"важность {f.min_importance or 1}–{f.max_importance or 5}")
        return "; ".join(p.strip() for p in parts) or None
