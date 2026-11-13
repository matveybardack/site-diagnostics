from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from site_diag.exceptions import AppError

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    """Сигналы рабочей задачи. Создаётся в потоке интерфейса."""

    finished = Signal(object)  # результат функции
    failed = Signal(object)  # исключение (Exception)
    _done = Signal(object)  # служебный: задача завершена, в аргументе сама задача


class Worker(QRunnable):
    """Выполняет функцию в потоке из QThreadPool.

    Функция не должна обращаться к виджетам: только к сервисам и данным.
    Результат или исключение возвращаются сигналами в поток интерфейса.
    """

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = WorkerSignals()
        # объект должен жить, пока сигналы не доставлены: удаляет его _Reaper
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            if isinstance(exc, AppError):
                log.warning("Фоновая задача завершилась ошибкой: %s", exc, exc_info=True)
            else:
                log.exception("Неожиданная ошибка в фоновой задаче")
            self._emit(self.signals.failed, exc)
        else:
            self._emit(self.signals.finished, result)
        finally:
            self._emit(self.signals._done, self)

    @staticmethod
    def _emit(signal: Any, value: Any) -> None:
        try:
            signal.emit(value)
        except RuntimeError:
            pass  # QObject уже уничтожен (закрытие приложения)


class _Reaper(QObject):
    """Хранит ссылки на выполняющиеся задачи и освобождает их после доставки сигналов."""

    def __init__(self) -> None:
        super().__init__()
        self._active: set[Worker] = set()

    def add(self, worker: Worker) -> None:
        self._active.add(worker)

    @Slot(object)
    def reap(self, worker: Worker) -> None:
        # слот выполняется в потоке интерфейса уже после finished/failed:
        # очередь сигналов одного отправителя сохраняет порядок
        self._active.discard(worker)


_reaper: _Reaper | None = None


def submit(worker: Worker, pool: QThreadPool | None = None) -> Worker:
    """Запускает задачу. Вызывать только из потока интерфейса.

    Подписываться на worker.signals.finished и worker.signals.failed нужно ДО
    вызова submit(). Обработчики должны быть методами QObject (виджета): тогда
    Qt вызывает их в потоке этого объекта, то есть интерфейса, и сам отключает
    обработчик, если объект уничтожен.
    """
    global _reaper
    if _reaper is None:
        _reaper = _Reaper()  # создаётся в потоке интерфейса, слот reap живёт там же
    _reaper.add(worker)
    worker.signals._done.connect(_reaper.reap)
    (pool or QThreadPool.globalInstance()).start(worker)
    return worker
