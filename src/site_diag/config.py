from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from site_diag.exceptions import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_VARS = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "LOG_LEVEL",
    "AUTO_MIGRATE",
)

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    log_level: str
    auto_migrate: bool
    project_root: Path = PROJECT_ROOT

    @property
    def dsn(self) -> str:
        """Строка подключения для psycopg (libpq key=value)."""
        return (
            f"host={self.db_host} port={self.db_port} "
            f"dbname={self.db_name} user={self.db_user} "
            f"password={self.db_password}"
        )

    @property
    def model_path(self) -> Path:
        return self.project_root / "model" / "site_failure_bn.json"

    @property
    def thresholds_path(self) -> Path:
        return self.project_root / "model" / "thresholds.json"

    @property
    def migrations_dir(self) -> Path:
        return self.project_root / "migrations"

    @property
    def log_file(self) -> Path:
        return self.project_root / "site_diag.log"

    def __repr__(self) -> str:
        return (
            f"Settings(db_host={self.db_host!r}, db_port={self.db_port}, "
            f"db_name={self.db_name!r}, db_user={self.db_user!r}, "
            f"log_level={self.log_level!r}, auto_migrate={self.auto_migrate})"
        )


def load_settings(env_file: Path | None = None) -> Settings:
    """Читает .env и переменные окружения; при проблемах бросает ConfigError."""
    path = env_file or (PROJECT_ROOT / ".env")
    if not path.exists():
        raise ConfigError(
            f"Файл конфигурации не найден: {path}. "
            "Создайте его из .env.example."
        )
    load_dotenv(path, override=False)

    missing = [v for v in REQUIRED_VARS if not os.getenv(v, "").strip()]
    if missing:
        raise ConfigError(
            "Не заданы переменные окружения: " + ", ".join(missing)
        )

    try:
        db_port = int(os.environ["DB_PORT"])
    except ValueError:
        raise ConfigError("DB_PORT должен быть целым числом") from None

    log_level = os.environ["LOG_LEVEL"].strip().upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL должен быть одним из: {', '.join(sorted(_VALID_LOG_LEVELS))}"
        )

    raw_migrate = os.environ["AUTO_MIGRATE"].strip().lower()
    if raw_migrate in _TRUE:
        auto_migrate = True
    elif raw_migrate in _FALSE:
        auto_migrate = False
    else:
        raise ConfigError("AUTO_MIGRATE должен быть true или false")

    return Settings(
        db_host=os.environ["DB_HOST"].strip(),
        db_port=db_port,
        db_name=os.environ["DB_NAME"].strip(),
        db_user=os.environ["DB_USER"].strip(),
        db_password=os.environ["DB_PASSWORD"],
        log_level=log_level,
        auto_migrate=auto_migrate,
    )
