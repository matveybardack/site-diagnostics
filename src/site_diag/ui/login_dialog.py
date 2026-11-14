from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from site_diag.domain.models import User
from site_diag.exceptions import AccessDeniedError
from site_diag.services.auth_service import AuthService
from site_diag.ui.diagnosis_view import user_text
from site_diag.ui.workers import Worker, submit

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class LoginDialog(QDialog):
    """Вход администратора: логин, скрытый пароль, не более 3 неудачных попыток.

    После успешного входа dialog.user содержит пользователя, и exec() возвращает
    Accepted. После трёх неудач форма блокируется до закрытия окна.
    Ошибка связи с БД не считается неудачной попыткой.
    """

    def __init__(self, auth: AuthService, parent: QWidget | None = None):
        super().__init__(parent)
        self._auth = auth
        self._failures = 0
        self._busy = False
        self._locked = False
        self.user: User | None = None

        self.setWindowTitle("Вход администратора")
        self.setModal(True)
        self.setMinimumWidth(360)

        self._login = QLineEdit()
        self._login.setPlaceholderText("Логин")
        self._login.setMaxLength(64)  # как в таблице users

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)  # ввод скрыт
        self._password.setPlaceholderText("Пароль")

        form = QFormLayout()
        form.addRow("Логин:", self._login)
        form.addRow("Пароль:", self._password)

        self._message = QLabel()
        self._message.setObjectName("loginError")
        self._message.setWordWrap(True)
        self._message.hide()

        self._login_button = QPushButton("Войти")
        self._login_button.setDefault(True)  # Enter отправляет форму
        self._login_button.clicked.connect(self._submit)
        self._cancel_button = QPushButton("Отмена")
        self._cancel_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._cancel_button)
        buttons.addWidget(self._login_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._message)
        layout.addLayout(buttons)

        self._login.returnPressed.connect(self._password.setFocus)
        self._password.returnPressed.connect(self._submit)
        QWidget.setTabOrder(self._login, self._password)
        QWidget.setTabOrder(self._password, self._login_button)
        QWidget.setTabOrder(self._login_button, self._cancel_button)

    @staticmethod
    def ask(auth: AuthService, parent: QWidget | None = None) -> User | None:
        """Показывает диалог. Возвращает пользователя или None, если вход не выполнен."""
        dialog = LoginDialog(auth, parent)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        user = dialog.user if accepted else None
        dialog.deleteLater()
        return user

    # отправка

    def _submit(self) -> None:
        if self._busy or self._locked:
            return
        login = self._login.text().strip()
        password = self._password.text()
        if not login or not password:
            self._show_error("Введите логин и пароль.")  # попытка не расходуется
            return

        self._set_busy(True)
        self._message.hide()
        worker = Worker(self._auth.login, login, password)
        worker.signals.finished.connect(self._on_success)
        worker.signals.failed.connect(self._on_failure)
        submit(worker)

    def _on_success(self, user: User) -> None:
        self._set_busy(False)
        self.user = user
        self._password.clear()
        self.accept()

    def _on_failure(self, exc: Exception) -> None:
        self._set_busy(False)
        self._password.clear()
        self._password.setFocus()

        if isinstance(exc, AccessDeniedError):
            self._failures += 1
            left = MAX_ATTEMPTS - self._failures
            if left > 0:
                self._show_error(f"{user_text(exc)} Осталось попыток: {left}.")
            else:
                self._lock()
        else:
            # связь с БД и прочие сбои: это не вина пользователя, попытка не считается
            self._show_error(user_text(exc))

    # состояние

    def _lock(self) -> None:
        self._locked = True
        self._login.setEnabled(False)
        self._password.setEnabled(False)
        self._login_button.setEnabled(False)
        self._cancel_button.setText("Закрыть")
        self._cancel_button.setFocus()
        self._show_error(
            f"Превышено число попыток входа ({MAX_ATTEMPTS}). "
            "Закройте окно и повторите позже."
        )
        log.warning("Исчерпан лимит попыток входа (%d)", MAX_ATTEMPTS)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (self._login, self._password, self._login_button):
            widget.setEnabled(not busy)
        self._cancel_button.setEnabled(not busy)
        self._login_button.setText("Проверка…" if busy else "Войти")

    def _show_error(self, text: str) -> None:
        self._message.setText(text)
        self._message.show()

    def reject(self) -> None:
        if self._busy:  # результат проверки ещё придёт; закрываться во время неё нельзя
            return
        super().reject()
