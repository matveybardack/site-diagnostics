#!/usr/bin/env python
"""Наполнение БД демонстрационными запросами диагностики (H-8).

Запуск:  python scripts/seed_demo_data.py --count 500

Для каждого запроса выбирается «истинная» причина (по заданным долям), симптомы
разыгрываются из таблиц условных вероятностей модели, а результат считает настоящий
вывод (DiagnosisService.diagnose). Запись идёт через DiagnosisRepository.save, как и
при обычной диагностике. Время запросов неравномерно по часам суток и дням недели.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# безвредное предупреждение внутри pgmpy; фильтр должен стоять до импорта pgmpy
warnings.filterwarnings("ignore", category=FutureWarning, module="pgmpy")

import psycopg.errors  # noqa: E402

from site_diag.config import Settings, load_settings  # noqa: E402
from site_diag.data.audit_repository import AuditRepository  # noqa: E402
from site_diag.data.connection import Database  # noqa: E402
from site_diag.data.diagnosis_repository import DiagnosisRepository  # noqa: E402
from site_diag.data.reference_repository import ReferenceRepository  # noqa: E402
from site_diag.domain.models import DiagnosisResult  # noqa: E402
from site_diag.exceptions import AppError  # noqa: E402
from site_diag.inference.engine import InferenceEngine  # noqa: E402
from site_diag.inference.interpretation import load_thresholds  # noqa: E402
from site_diag.inference.network_loader import load_network  # noqa: E402
from site_diag.logging_setup import setup_logging  # noqa: E402
from site_diag.services.diagnosis_service import DiagnosisService  # noqa: E402

DEFAULT_COUNT = 500
DEFAULT_DAYS = 30
MAX_COUNT = 100_000
MAX_DAYS = 365

# доля причин среди демонстрационных запросов (веса, не обязательно в сумме 100)
CAUSE_WEIGHTS: dict[str, float] = {
    "server_overload": 20,
    "bad_release": 15,
    "tls_cert_expired": 10,
    "dns_failure": 10,
    "site_db_failure": 10,
    "cdn_failure": 8,
    "webserver_misconfig": 8,
    "geo_ip_block": 7,
    "disk_full": 6,
    "ddos_attack": 6,
}
DEFAULT_CAUSE_WEIGHT = 5  # причина, которой нет в таблице выше

# вес часа суток 0-23 (по часовому поясу отчётов): днём обращений больше, ночью мало
HOUR_WEIGHTS = (
    1, 1, 1, 1, 1, 2,
    3, 5, 8, 10, 11, 10,
    8, 9, 10, 10, 9, 8,
    6, 5, 4, 3, 2, 1,
)
# вес дня недели (пн..вс): в выходные заметно тише
WEEKDAY_WEIGHTS = (1.0, 1.0, 1.0, 1.0, 1.0, 0.5, 0.4)

NOISE_PROBABILITY = 0.05  # пользователь отметил лишний симптом
FREE_TEXT_PROBABILITY = 0.10
FREE_TEXTS = (
    "Клиенты жалуются, что сайт не открывается.",
    "Началось около часа назад, раньше всё работало.",
    "Страница грузится бесконечно, потом ошибка.",
    "Поддержка получила несколько обращений подряд.",
    "Проверял с телефона и с ноутбука, везде одинаково.",
    "Коллеги из другого офиса тоже не могут открыть сайт.",
)
_MAX_SAMPLE_TRIES = 20
_MAX_TIME_TRIES = 50


@dataclass(frozen=True, slots=True)
class PlannedRequest:
    when: datetime  # aware, в часовом поясе отчётов
    cause: str  # «истинная» причина, из которой разыграны симптомы
    codes: tuple[str, ...]
    free_text: str | None


# (без обращений к БД)


def build_profiles(engine: InferenceEngine) -> dict[str, tuple[list[str], list[float]]]:
    """Для каждой причины: симптомы и P(симптом наблюдается | причина) из модели."""
    symptoms = sorted(engine.symptom_codes)
    return {
        cause: (symptoms, [engine.present_probability(s, cause) for s in symptoms])
        for cause in engine.cause_codes
    }


def sample_symptoms(
    rng: random.Random, symptoms: list[str], probs: list[float]
) -> tuple[str, ...]:
    """Набор симптомов для одной причины; всегда не пустой."""
    codes: list[str] = []
    for _ in range(_MAX_SAMPLE_TRIES):
        codes = [s for s, p in zip(symptoms, probs) if rng.random() < p]
        if codes:
            break
    else:  # крайне маловероятно: берём самый типичный симптом
        codes = [symptoms[max(range(len(probs)), key=probs.__getitem__)]]

    if rng.random() < NOISE_PROBABILITY:
        extra = rng.choice(symptoms)
        if extra not in codes:
            codes.append(extra)
    return tuple(sorted(codes))


def random_time(
    rng: random.Random, zone: ZoneInfo, days: int, now: datetime
) -> datetime:
    """Момент в последние `days` суток с суточной и недельной неравномерностью."""
    today = now.astimezone(zone).date()
    candidates: list[date] = [today - timedelta(days=i) for i in range(days)]
    day_weights = [WEEKDAY_WEIGHTS[d.weekday()] for d in candidates]
    for _ in range(_MAX_TIME_TRIES):
        day = rng.choices(candidates, day_weights)[0]
        hour = rng.choices(range(24), HOUR_WEIGHTS)[0]
        when = datetime(
            day.year, day.month, day.day, hour,
            rng.randrange(60), rng.randrange(60), tzinfo=zone,
        )
        if when <= now:  # в будущее запросы не ставим (бывает только для сегодняшнего дня)
            return when
    return now - timedelta(minutes=rng.randrange(1, 60))


def build_plan(
    rng: random.Random,
    engine: InferenceEngine,
    zone: ZoneInfo,
    count: int,
    days: int,
    now: datetime,
) -> list[PlannedRequest]:
    """План запросов по возрастанию времени (id в БД растут вместе со временем)."""
    profiles = build_profiles(engine)
    causes = list(engine.cause_codes)
    weights = [CAUSE_WEIGHTS.get(c, DEFAULT_CAUSE_WEIGHT) for c in causes]

    plan = []
    for _ in range(count):
        cause = rng.choices(causes, weights)[0]
        symptoms, probs = profiles[cause]
        plan.append(
            PlannedRequest(
                when=random_time(rng, zone, days, now),
                cause=cause,
                codes=sample_symptoms(rng, symptoms, probs),
                free_text=(
                    rng.choice(FREE_TEXTS) if rng.random() < FREE_TEXT_PROBABILITY else None
                ),
            )
        )
    plan.sort(key=lambda p: p.when)
    return plan


# запись


def seed(settings: Settings, count: int, days: int, seed_value: int | None) -> int:
    rng = random.Random(seed_value)
    zone = ZoneInfo(settings.report_timezone)
    db = Database(settings.dsn)
    done = 0
    try:
        reference_repo = ReferenceRepository(db)
        repo = DiagnosisRepository(db)
        engine = InferenceEngine(*load_network(settings.model_path))
        service = DiagnosisService(
            engine,
            load_thresholds(settings.thresholds_path),
            repo,
            AuditRepository(db),
            reference_repo=reference_repo,
        )
        service.check_consistency()
        cause_labels = {c.code: c.label for c in reference_repo.list_causes()}

        plan = build_plan(rng, engine, zone, count, days, datetime.now(zone))
        print(f"Запись {count} запросов за последние {days} дн. (пояс {settings.report_timezone})…")

        # результат зависит только от набора симптомов: одинаковые наборы считаем один раз
        cache: dict[tuple[str, ...], DiagnosisResult] = {}
        leaders: Counter[str] = Counter()
        certainty: Counter[str] = Counter()
        step = max(1, count // 10)

        for item in plan:
            result = cache.get(item.codes)
            if result is None:
                result = cache[item.codes] = service.diagnose(item.codes)
            repo.save(result, item.codes, item.when, item.free_text)
            done += 1
            leaders[result.leader_code] += 1
            certainty[result.certainty] += 1
            if done % step == 0 or done == count:
                print(f"  {done}/{count}", flush=True)

        print(f"\nГотово: записано {done} запросов; разных наборов симптомов {len(cache)}.")
        print("Лидирующая причина по результатам вывода:")
        for code, n in leaders.most_common():
            print(f"  {cause_labels.get(code, code):<45} {n:>6}  ({n / done:.0%})")
        print(
            "Определённость: "
            + ", ".join(f"{name} {n}" for name, n in sorted(certainty.items()))
        )
        return done
    except BaseException:
        print(f"\nПрервано: записано {done} из {count}.", file=sys.stderr)
        raise
    finally:
        db.close()


# командная строка


def _int_in_range(low: int, high: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"нужно целое число, получено {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"допустимо от {low} до {high}")
        return value

    return parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Наполнение БД демонстрационными запросами.")
    parser.add_argument(
        "--count", type=_int_in_range(1, MAX_COUNT), default=DEFAULT_COUNT,
        help=f"сколько запросов добавить (по умолчанию {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--days", type=_int_in_range(1, MAX_DAYS), default=DEFAULT_DAYS,
        help=f"за сколько последних суток распределить запросы (по умолчанию {DEFAULT_DAYS})",
    )
    parser.add_argument("--seed", type=int, default=None, help="начальное значение генератора (для повторяемости)")
    parser.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings()
        setup_logging(settings)
    except AppError as exc:
        print(f"Ошибка: {exc.user_message}\n{exc}", file=sys.stderr)
        return 1
    # вывод для каждого запроса в лог не нужен: тысячи одинаковых строк
    logging.getLogger("site_diag.services.diagnosis_service").setLevel(logging.WARNING)

    if not args.yes:
        answer = input(
            f"Добавить {args.count} демонстрационных запросов в БД "
            f"«{settings.db_name}» ({settings.db_host}:{settings.db_port})? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return 0

    try:
        seed(settings, args.count, args.days, args.seed)
    except AppError as exc:
        print(f"Ошибка: {exc.user_message}\n{exc}", file=sys.stderr)
        return 1
    except psycopg.errors.UndefinedTable:
        print("Ошибка: схема БД не создана. Выполните: python scripts/init_db.py", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
