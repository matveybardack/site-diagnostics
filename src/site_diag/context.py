from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from site_diag.config import Settings
from site_diag.domain.models import Cause, Symptom
from site_diag.services.auth_service import AuthService
from site_diag.services.diagnosis_service import DiagnosisService
from site_diag.services.report_service import ReportService


@dataclass(frozen=True, slots=True)
class AppContext:
    """Всё, что нужно интерфейсу. Собирается в main.build_context().

    UI видит только сервисы, справочники и два вспомогательных вызова:
    репозитории, пул соединений и движок вывода ему недоступны.
    """

    settings: Settings
    diagnosis: DiagnosisService
    auth: AuthService
    reports: ReportService
    symptoms: tuple[Symptom, ...]  # в порядке отображения в форме
    causes: tuple[Cause, ...]
    model_version: str
    is_db_available: Callable[[], bool]  # блокирующий вызов: только из Worker
    close: Callable[[], None]  # освобождает пул соединений
