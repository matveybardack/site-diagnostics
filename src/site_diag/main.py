from __future__ import annotations

import logging
import sys
import warnings

# безвредное предупреждение внутри pgmpy; фильтр должен стоять до импорта pgmpy
warnings.filterwarnings("ignore", category=FutureWarning, module="pgmpy")

from PySide6.QtWidgets import QApplication  # noqa: E402

from site_diag.config import Settings, load_settings  # noqa: E402
from site_diag.context import AppContext  # noqa: E402
from site_diag.data.audit_repository import AuditRepository  # noqa: E402
from site_diag.data.connection import Database  # noqa: E402
from site_diag.data.diagnosis_repository import DiagnosisRepository  # noqa: E402
from site_diag.data.migrator import Migrator  # noqa: E402
from site_diag.data.reference_repository import ReferenceRepository  # noqa: E402
from site_diag.data.report_queries import ReportQueries  # noqa: E402
from site_diag.data.user_repository import UserRepository  # noqa: E402
from site_diag.exceptions import AppError  # noqa: E402
from site_diag.inference.engine import InferenceEngine  # noqa: E402
from site_diag.inference.interpretation import load_thresholds  # noqa: E402
from site_diag.inference.network_loader import load_network  # noqa: E402
from site_diag.logging_setup import setup_logging  # noqa: E402
from site_diag.services.auth_service import AuthService  # noqa: E402
from site_diag.services.diagnosis_service import DiagnosisService  # noqa: E402
from site_diag.services.report_service import ReportService  # noqa: E402
from site_diag.ui.main_window import MainWindow  # noqa: E402

log = logging.getLogger(__name__)


def build_context(settings: Settings) -> AppContext:
    """Собирает все объекты приложения. Единственное место, где они создаются.

    Порядок: миграции (если AUTO_MIGRATE) → пул соединений → репозитории →
    модель и движок → сервисы → проверка согласованности. Миграции идут до
    создания пула, потому что мигратор открывает своё соединение.

    Бросает AppError (ConfigError, DatabaseUnavailableError, MigrationError,
    ModelError): при любой из них запуск невозможен.
    """
    if settings.auto_migrate:
        Migrator(settings.dsn, settings.migrations_dir).apply_pending()

    db = Database(settings.dsn)
    try:
        reference_repo = ReferenceRepository(db)
        audit_repo = AuditRepository(db)

        engine = InferenceEngine(*load_network(settings.model_path))
        thresholds = load_thresholds(settings.thresholds_path)

        symptoms = tuple(reference_repo.list_symptoms())
        causes = tuple(reference_repo.list_causes())

        diagnosis = DiagnosisService(
            engine,
            thresholds,
            DiagnosisRepository(db),
            audit_repo,
            symptom_labels={s.code: s.label for s in symptoms},
            cause_labels={c.code: c.label for c in causes},
            reference_repo=reference_repo,
        )
        diagnosis.check_consistency()

        return AppContext(
            settings=settings,
            diagnosis=diagnosis,
            auth=AuthService(UserRepository(db), audit_repo),
            reports=ReportService(
                ReportQueries(db), audit_repo, settings.report_timezone
            ),
            symptoms=symptoms,
            causes=causes,
            model_version=engine.version,
            is_db_available=db.is_available,
            close=db.close,
        )
    except BaseException:
        db.close()  # при сбое сборки пул не должен остаться открытым
        raise


def main() -> int:
    try:
        settings = load_settings()
        setup_logging(settings)
    except AppError as exc:
        print(f"Ошибка: {exc.user_message}\n{exc}", file=sys.stderr)
        return 1

    log.info("Запуск приложения: %s", settings)
    try:
        ctx = build_context(settings)
    except AppError as exc:
        log.error("Запуск невозможен: %s", exc, exc_info=True)
        print(f"Ошибка: {exc.user_message}\n{exc}", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.aboutToQuit.connect(ctx.close)
    window = MainWindow(ctx)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
