from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

_QSS_PATH = Path(__file__).with_name("styles.qss")


def load_stylesheet() -> str:
    try:
        return _QSS_PATH.read_text(encoding="utf-8")
    except OSError:
        # без файла стилей приложение работает, но выглядит стандартно
        log.warning("Не удалось прочитать %s", _QSS_PATH, exc_info=True)
        return ""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")  # одинаковый вид на Windows, macOS и Linux
    app.setStyleSheet(load_stylesheet())


def set_state(widget: QWidget, name: str, value: str) -> None:
    """Меняет динамическое свойство и перерисовывает виджет по новому правилу QSS.

    Без unpolish/polish Qt не применяет изменённое свойство к уже показанному виджету.
    """
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
